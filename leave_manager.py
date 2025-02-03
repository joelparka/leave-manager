import os

import logging

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

# 4) Slack 연동 기능 - 메시지 전송

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

# 5) Slack 명령어: `/연차몇개`

# ------------------------------------------------------------------

@app.route("/slack/command", methods=["POST"])

def slash_command():

    try:

        data = request.form

        command = data.get("command", "")

        user = data.get("user_name", "")


        if command == "/연차몇개":

            resp = sheet.values().get(spreadsheetId=GOOGLE_SHEET_ID, range="VACATION!A2:E").execute()

            rows = resp.get("values", [])

            row = next((r for r in rows if r[0].lower() == user.lower()), None)


            remain = row[4] if row and len(row) > 4 else "0"

            return jsonify({"response_type": "ephemeral", "text": f"{user}님의 잔여 연차는 {remain}일입니다."}), 200


    except Exception as e:

        logging.error(f"slash_command error: {e}")

        return jsonify({"text": f"⚠️ 오류: {str(e)}"}), 500


# ------------------------------------------------------------------

# 6) Slack 이벤트: 이모지 반응 감지

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


                if ts in pending_requests:

                    nickname, req_days = pending_requests[ts]

                    add = reaction in ("흰색_확인_표시", "white_check_mark")


                    update_leave(nickname, req_days, add=add, channel_id=channel_id)


        return jsonify({"status": "ok"}), 200


    except Exception as e:

        logging.error(f"Error in /slack/events: {e}")

        return jsonify({"status": "error"}), 500


if __name__ == "__main__":

    app.run(host="0.0.0.0", port=3000)

