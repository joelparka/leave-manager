import os
import logging
import requests  # 추가
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
import datetime
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.DEBUG)

# ------------------------------------------------------------------
# 1) Slack & Google Sheets 설정 (.env에서 불러오기)
# ------------------------------------------------------------------
SLACK_BOT_TOKEN = os.getenv("SLACK_API_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")

slack_client = WebClient(token=SLACK_BOT_TOKEN)

creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)
service = build('sheets', 'v4', credentials=creds)
sheet = service.spreadsheets()

# ------------------------------------------------------------------
# 시트 구조: A=닉네임, B=입사일, C=연차개수, D=소진량, E=잔여량
# ------------------------------------------------------------------
pending_requests = {}

# ------------------------------------------------------------------
# 2) 1년 미만 -> C열=근무개월 / 1년 이상 -> C열(사용자 직접 입력)
# ------------------------------------------------------------------
def months_worked(date_str):
    try:
        d = datetime.datetime.strptime(date_str, "%Y.%m.%d").date()
    except ValueError:
        return 0
    today = datetime.date.today()
    return (today.year - d.year) * 12 + (today.month - d.month)

def recalc_and_save(rows):
    new_rows = []
    for row in rows:
        while len(row) < 5:
            row.append("0")
        join_date_str = row[1]
        used_str = row[3]
        try:
            used_leaves = float(used_str)
        except ValueError:
            used_leaves = 0.0
            row[3] = "0"
        m = months_worked(join_date_str)
        if m < 12:
            row[2] = str(m)
        try:
            c_val = float(row[2])
        except ValueError:
            c_val = 0.0
            row[2] = "0"
        remain = c_val - used_leaves
        row[4] = str(remain)
        new_rows.append(row)
    return new_rows

def save_rows_to_sheet(rows):
    sheet.values().update(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="VACATION!A2:E",
        valueInputOption="RAW",
        body={"values": rows}
    ).execute()

# ------------------------------------------------------------------
# 3) 날짜포맷: YYMMDD -> "YY년MM월DD일"
# ------------------------------------------------------------------
def convert_yyMMdd_format(yyMMdd):
    if len(yyMMdd) != 6:
        return yyMMdd
    yy, mm, dd = yyMMdd[:2], yyMMdd[2:4], yyMMdd[4:6]
    return f"{yy}년{mm}월{dd}일"

# ------------------------------------------------------------------
# 4) Slack에 메시지 전송하기 (참고용)
# ------------------------------------------------------------------
def send_slack_message(channel, message):
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "channel": channel,
        "text": message
    }
    response = requests.post(url, json=payload, headers=headers)
    return response.json()

# ------------------------------------------------------------------
# (추가) 5) 연차 승인/반려 처리 함수 (2번 코드에서 가져옴)
# ------------------------------------------------------------------
def update_leave(nickname, requested_days, add, channel_id, user_who_clicked):
    """
    nickname: 요청자 닉네임
    requested_days: 신청한 연차 수
    add=True => 승인 => 소진량 += requested_days
        False => 반려 => 소진량 -= requested_days (2번 코드에서는 reaction_removed 시 로직이 이렇게 동작)
    channel_id: 메시지 채널
    user_who_clicked: 이모지를 누른 Slack 사용자
    """
    try:
        resp = sheet.values().get(spreadsheetId=GOOGLE_SHEET_ID, range="VACATION!A2:E").execute()
        rows = resp.get("values", [])

        updated_rows = []
        target_row_idx = None
        old_used = None
        found = False

        for i, row in enumerate(rows):
            if len(row) > 0 and row[0].lower() == nickname.lower():
                found = True
                if len(row) < 5:
                    while len(row) < 5:
                        row.append("0")
                try:
                    used_val = float(row[3])  # 기존 소진량
                except ValueError:
                    used_val = 0.0
                old_used = used_val
                if add:
                    new_used = used_val + requested_days
                else:
                    new_used = used_val - requested_days
                row[3] = str(new_used)
                target_row_idx = i
            updated_rows.append(row)

        if not found:
            logging.warning(f"Nickname {nickname} not found in sheet. Abort.")
            return

        # 재계산
        final_rows = recalc_and_save(updated_rows)
        # 만약 E열이 음수가 되었다면 => 승인 불가 => 되돌림 => ephemeral 안내
        changed_row = final_rows[target_row_idx]
        remain_val = float(changed_row[4]) if len(changed_row) > 4 else 0.0
        if remain_val < 0:
            # 되돌림
            changed_row[3] = str(old_used)
            final_rows = recalc_and_save(final_rows)

            # ephemeral 안내
            slack_client.chat_postEphemeral(
                channel=channel_id,
                user=user_who_clicked,
                text=(
                    f"연차를 다 써서 승인 불가합니다 ㅠㅠ\n"
                    f"{nickname}님의 남은 연차 갯수: {changed_row[4]}일"
                )
            )
            logging.info(f"Rejected {nickname}'s request because remain<0. Reverted.")
        else:
            logging.info(f"update_leave success => nick={nickname}, add={add}, days={requested_days}")

        save_rows_to_sheet(final_rows)

    except Exception as e:
        logging.error(f"Error in update_leave: {e}")

# ------------------------------------------------------------------
# 6) Slack 명령어 처리
# ------------------------------------------------------------------
@app.route("/slack/command", methods=["POST"])
def slash_command():
    try:
        data = request.form
        command = data.get("command", "")
        text = data.get("text", "")
        user = data.get("user_name", "")
        channel_id = data.get("channel_id", "")

        if command == "/연차몇개":
            resp = sheet.values().get(spreadsheetId=GOOGLE_SHEET_ID, range="VACATION!A2:E").execute()
            rows = resp.get("values", [])
            row = next((r for r in rows if r[0].lower() == user.lower()), None)

            remain = row[4] if row and len(row) > 4 else "0"
            return jsonify({"response_type": "ephemeral", "text": f"{user}님의 잔여 연차는 {remain}일입니다."}), 200

        elif command == "/연차":
            # /연차 YYMMDD/연차갯수/비고/@승인자
            parts = text.split("/")
            if len(parts) != 4:
                return jsonify({
                    "response_type": "ephemeral",
                    "text": "⚠️ 올바른 서식: `YYMMDD/연차갯수/비고/@승인자`"
                }), 200

            date_str, days_str, reason, approver = parts
            date_formatted = convert_yyMMdd_format(date_str)

            if approver.startswith("@"):
                mention_approver = f"<{approver}>"
            else:
                mention_approver = approver
            mention_writer = f"<@{user}>"

            try:
                requested_days = float(days_str)
            except ValueError:
                return jsonify({
                    "response_type": "ephemeral",
                    "text": "⚠️ 연차갯수는 숫자여야 합니다."
                }), 200

            # 채널 메시지
            resp_msg = slack_client.chat_postMessage(
                channel=channel_id,
                text=(
                    f"★연차요청★\n"
                    f"일시: {date_formatted}\n"
                    f"요청연차: {requested_days}\n"
                    f"비고: {reason}\n"
                    f"컨펌자: {mention_approver}\n"
                    f"작성자: {mention_writer}"
                )
            )
            if not resp_msg["ok"]:
                logging.error(f"chat_postMessage error: {resp_msg['error']}")
                return jsonify({"text": f"메시지 전송 실패: {resp_msg['error']}"}), 500

            ts = resp_msg["ts"]
            pending_requests[ts] = (user, requested_days)

            return jsonify({
                "response_type": "ephemeral",
                "text": "연차 요청이 접수되었습니다."
            }), 200

        else:
            return jsonify({"text": f"알 수 없는 명령어: {command}"}), 400

    except Exception as e:
        logging.error(f"slash_command error: {e}")
        return jsonify({"text": f"⚠️ 오류: {str(e)}"}), 500

# ------------------------------------------------------------------
# 7) Slack 이벤트: 이모지 반응 감지
# ------------------------------------------------------------------
@app.route("/slack/events", methods=["POST"])
def slack_events():
    try:
        data = request.json

        if data.get("type") == "url_verification":
            return jsonify({"challenge": data.get("challenge")}), 200

        if "event" in data:
            event = data["event"]
            if event.get("type") in ("reaction_added", "reaction_removed"):
                reaction = event["reaction"]
                ts = event["item"]["ts"]
                channel_id = event["item"]["channel"]
                user_who_clicked = event["user"]  # 이모지를 누른 사람

                if ts in pending_requests:
                    nickname, req_days = pending_requests[ts]

                    # reaction_added => add=True, reaction_removed => add=False
                    add = (event["type"] == "reaction_added")

                    # 2번 코드처럼 white_check_mark / 흰색_확인_표시 => 승인
                    # x => 반려(승인 반대)
                    if reaction in ("흰색_확인_표시", "white_check_mark"):
                        update_leave(
                            nickname, req_days,
                            add=add,
                            channel_id=channel_id,
                            user_who_clicked=user_who_clicked
                        )
                    elif reaction == "x":
                        # 반려
                        update_leave(
                            nickname, req_days,
                            add=not add,  # reaction_added + x => 반려
                            channel_id=channel_id,
                            user_who_clicked=user_who_clicked
                        )

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.error(f"Error in /slack/events: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
