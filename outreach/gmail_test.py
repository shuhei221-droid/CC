"""One real diagnostic message per mailbox/allowlisted recipient; never a sales job."""
import base64
import os
import re
import time
from email.message import EmailMessage
from email.utils import parseaddr
from urllib.parse import quote

from fastapi import APIRouter
from . import auth, delivery, gmail_connect as gmail
from .db import atomic, audit, get_db

bp = APIRouter()
SUBJECT = "【LEED Outreach OS】Gmail連携テスト"
BODY = "Gmail APIの送信確認メールです。受信できましたら「受信しました」と返信してください。"


def recipients():
    values = sorted(set(x.strip().lower() for x in os.getenv("GMAIL_TEST_RECIPIENTS", "").split(",") if x.strip()))
    if len(values) > 10 or any(not delivery.valid_email(x) for x in values):
        gmail.fail("GMAIL_TEST_RECIPIENTSには管理するテスト先を10件以内で設定してください。", 503)
    return values


def permitted(recipient):
    if not isinstance(recipient, str) or recipient not in recipients():
        gmail.fail("許可済みのテスト先を選択してください。", 403)
    blocked = get_db().execute("""SELECT 1 FROM contacts c JOIN suppressions s ON s.company_id=c.company_id
        WHERE c.kind='email' AND lower(c.value)=? LIMIT 1""", (recipient,)).fetchone()
    if blocked:
        gmail.fail("送信停止中の宛先にはテスト送信できません。", 409)


def public(row):
    return {k: row[k] for k in ("id", "mailbox", "recipient", "subject", "body", "status", "created_at",
                                "sent_at", "message_id", "reply_count", "checked_at")}


def find(test_id):
    if not isinstance(test_id, str) or not re.fullmatch(r"[a-f0-9]{64}", test_id):
        gmail.fail("テストを選択してください。")
    row = get_db().execute("SELECT * FROM gmail_diagnostics WHERE id=?", (test_id,)).fetchone()
    if not row:
        gmail.fail("テストが見つかりません。", 404)
    return row


@bp.get("/api/gmail/test/status")
def status():
    return {"recipients": recipients(), "subject": SUBJECT, "body": BODY,
            "history": [public(r) for r in get_db().execute("SELECT * FROM gmail_diagnostics ORDER BY created_at DESC LIMIT 50")]}


@bp.post("/api/gmail/test/preview")
def preview(data: dict):
    recipient = data.get("recipient")
    permitted(recipient)
    session = delivery.gmail_session()
    if session["mailbox"] != delivery.cfg("SENDER_EMAIL").lower():
        gmail.fail("Gmail接続先と送信元メールを一致させてください。", 409)
    test_id = auth.digest(repr(("gmail-diagnostic-v1", session["mailbox"], recipient)))
    expires = int(time.time()) + 600
    snapshot = auth.digest(repr((test_id, session["fingerprint"], SUBJECT, BODY, expires)))
    with atomic() as db:
        permitted(recipient)
        if gmail.connection_fingerprint() != session["fingerprint"] or delivery.cfg("SENDER_EMAIL").lower() != session["mailbox"]:
            gmail.fail("接続設定が変わりました。もう一度確認してください。", 409)
        db.execute("""INSERT INTO gmail_diagnostics(id,mailbox,recipient,subject,body,fingerprint,snapshot,expires,status)
            VALUES(?,?,?,?,?,?,?,?,'prepared') ON CONFLICT(id) DO NOTHING""",
            (test_id,session["mailbox"],recipient,SUBJECT,BODY,session["fingerprint"],snapshot,expires))
        row = find(test_id)
        if row["status"] != "prepared":
            gmail.fail("この送信元・宛先ではテスト実行済みです。履歴を確認してください。再送はしません。", 409)
        db.execute("UPDATE gmail_diagnostics SET fingerprint=?,snapshot=?,expires=? WHERE id=?",
                   (session["fingerprint"],snapshot,expires,test_id))
        audit("gmail.test_preview", {"id":test_id,"recipient":recipient})
    return {**public(find(test_id)), "snapshot":snapshot}


@bp.post("/api/gmail/test/send")
def send(data: dict):
    if data.get("confirm") is not True:
        gmail.fail("宛先・文面を確認してテスト送信を承認してください。")
    row = find(data.get("id"))
    permitted(row["recipient"])
    # A repeated call never authenticates or sends again, even after a timeout.
    if row["status"] != "prepared":
        return public(row)
    session = delivery.gmail_session()
    with atomic() as db:
        row = find(data.get("id"))
        if row["status"] != "prepared":
            return public(row)
        permitted(row["recipient"])
        if (row["snapshot"] != data.get("snapshot") or row["expires"] <= int(time.time()) or
            row["fingerprint"] != session["fingerprint"] or gmail.connection_fingerprint() != session["fingerprint"] or
            row["mailbox"] != session["mailbox"] or delivery.cfg("SENDER_EMAIL").lower() != row["mailbox"]):
            gmail.fail("確認内容が期限切れ、または接続設定が変更されました。宛先・文面を確認し直してください。", 409)
        db.execute("UPDATE gmail_diagnostics SET status='sending',approved_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
        audit("gmail.test_approved", {"id":row["id"],"recipient":row["recipient"],"snapshot":row["snapshot"]})
    # Claim is committed before contacting Gmail. Crashes remain 'sending'; no retry.
    try:
        mail = EmailMessage()
        mail["From"],mail["To"],mail["Subject"] = row["mailbox"],row["recipient"],row["subject"]
        mail["Message-ID"] = "<leed-test-"+row["id"]+"@"+row["mailbox"].split("@")[1]+">"
        mail.set_content(row["body"])
        result = delivery.api("POST", delivery.GMAIL+"/messages/send", session["token"],
                              body={"raw":base64.urlsafe_b64encode(mail.as_bytes()).decode()})
        if not all(isinstance(result.get(k),str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}",result[k]) for k in ("id","threadId")):
            raise ValueError("Missing receipt")
    except Exception:
        with atomic() as db:
            db.execute("UPDATE gmail_diagnostics SET status='unknown' WHERE id=?", (row["id"],))
            audit("gmail.test_unknown", {"id":row["id"]})
        return public(find(row["id"]))
    with atomic() as db:
        db.execute("""UPDATE gmail_diagnostics SET status='sent',message_id=?,thread_id=?,sent_at=CURRENT_TIMESTAMP WHERE id=?""",
                   (result["id"],result["threadId"],row["id"]))
        audit("gmail.test_sent", {"id":row["id"],"message_id":result["id"]})
    return public(find(row["id"]))


@bp.post("/api/gmail/test/replies")
def replies(data: dict):
    row = find(data.get("id"))
    if row["status"] != "sent" or not row["thread_id"]:
        gmail.fail("Gmailの送信受付が確認できたテストを選択してください。", 409)
    session = delivery.gmail_session()
    if session["mailbox"] != row["mailbox"]:
        gmail.fail("テストを送信したGmailアカウントに接続してください。", 409)
    thread = delivery.api("GET",delivery.GMAIL+"/threads/"+quote(row["thread_id"],safe="")+
                          "?format=metadata&metadataHeaders=From",session["token"])
    if thread.get("id") != row["thread_id"]:
        gmail.fail("返信スレッドを確認できません。", 502)
    ids = set()
    for message in thread.get("messages",[]):
        if not message.get("id") or message["id"] == row["message_id"] or "SENT" in message.get("labelIds",[]):
            continue
        headers = message.get("payload",{}).get("headers",[])
        values = [h.get("value","") for h in headers if h.get("name","").lower()=="from"]
        if len(values)==1 and parseaddr(values[0])[1].lower()==row["recipient"]:
            ids.add(message["id"])
    with atomic() as db:
        if gmail.connection_fingerprint() != session["fingerprint"]:
            gmail.fail("接続先が変更されました。もう一度確認してください。", 409)
        db.execute("UPDATE gmail_diagnostics SET reply_count=?,checked_at=CURRENT_TIMESTAMP WHERE id=?",(len(ids),row["id"]))
        audit("gmail.test_replies", {"id":row["id"],"count":len(ids)})
    return public(find(row["id"]))
