"""Authenticated management API and HTML entry point."""
import json
import os
import sqlite3
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from . import auth, exchange, records
from .db import atomic, audit, get_db, migrate
from .metrics import GROUPS, metrics

HERE = Path(__file__).parent


def rows(name, order="id", limit=None):
    sql=f"SELECT * FROM {name} ORDER BY {order}"
    if limit: sql+=f" LIMIT {int(limit)}"
    return [dict(x) for x in get_db().execute(sql)]


def create_app():
    from . import delivery, engine, worker, gmail_connect, gmail_test, pipeline, discovery, serverless
    auth.config()
    migrate()
    auth.initialize_admin()
    app=FastAPI(title="LEED Outreach OS",docs_url=None,redoc_url=None,openapi_url=None)
    app.middleware("http")(auth.guarded)
    app.include_router(auth.bp)
    app.include_router(exchange.bp)
    for module in (engine, worker, delivery, gmail_connect, gmail_test, pipeline, discovery, serverless):
        app.include_router(module.bp)
    app.mount("/static",StaticFiles(directory=HERE/"static"),name="assets")

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request,exc):
        return JSONResponse({"detail":"同一企業名・ドメイン・窓口の重複、またはデータ制約により保存できません。"},409)

    @app.exception_handler(sqlite3.OperationalError)
    async def database_busy(request,exc):
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return JSONResponse({"detail":"別の処理が保存中です。画面を更新して状態を確認してください。"},503,headers={"Retry-After":"2"})
        raise exc

    @app.exception_handler(ValueError)
    async def invalid(request,exc):
        return JSONResponse({"detail":str(exc)[:300]},400)

    @app.get("/health")
    def health():
        return {"status":"ok"}

    @app.get("/",response_class=HTMLResponse)
    def home(request:Request):
        return (HERE/"templates/index.html").read_text().replace("{{csrf}}",request.state.csrf).replace("{{ csrf }}",request.state.csrf)

    @app.get("/api/state")
    def state():
        analyses=rows("analyses","company_id,campaign_id")
        for a in analyses:
            for k in ("breakdown","facts","hypotheses"):
                a[k]=json.loads(a[k])
        contacts=rows("contacts")
        for row in contacts:
            row["effective_eligibility"]=("allowed" if delivery.usable_contact(row) else "review") if row["eligibility"]=="allowed" else row["eligibility"]
        return dict(companies=rows("companies"),contacts=contacts,evidence=rows("evidence"),campaigns=rows("campaigns"),
            pipelines=pipeline.recent(),discovery_origins=rows("company_discovery_origins","company_id,campaign_id"),memberships=rows("campaign_companies","campaign_id,company_id"),analyses=analyses,messages=rows("messages"),
            checks=rows("research_checks","id DESC",2000),review_sources=rows("initial_review_sources"),
            runs=rows("agent_runs","id DESC",150),audit=rows("audit_log","id DESC",150),
            settings={r["key"]:r["value"] for r in rows("settings","key")},
            metrics=metrics()["rows"][0])

    @app.get("/api/metrics")
    def kpis(group:str=""):
        if group and group not in GROUPS: records.bad("未対応の集計軸です。")
        return metrics(group)

    @app.post("/api/companies")
    def save_company(data:dict):
        with atomic():
            pk=records.company(data)
        return {"id":pk}

    @app.post("/api/companies/bulk")
    def bulk(data:dict):
        ids=data.get("ids")
        status=data.get("status")
        if not isinstance(ids,list) or not 1<=len(ids)<=2000 or status not in ("pending","approved","hold"):
            records.bad("対象と変更先を選択してください。")
        with atomic() as db:
            for pk in set(records.integer(i) for i in ids):
                records.entity("companies",pk)
                db.execute("UPDATE companies SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(status,pk))
            audit("company.bulk",{"count":len(set(ids)),"status":status})
        return {"count":len(set(ids))}

    @app.post("/api/memberships")
    def membership(data:dict):
        with atomic():
            records.member(data.get("company_id"),data.get("campaign_id"))
            audit("campaign.member",{"company_id":data.get("company_id"),"campaign_id":data.get("campaign_id")})
        return {"ok":True}

    @app.post("/api/campaigns")
    def save_campaign(data:dict):
        with atomic():
            pk=records.campaign(data)
        return {"id":pk}

    @app.post("/api/contacts")
    def add_contact(data:dict):
        with atomic():
            pk=records.contact(data)
        return {"id":pk}

    @app.post("/api/evidence")
    def add_evidence(data:dict):
        with atomic() as db:
            pk=records.entity("companies",data.get("company_id"))["id"]
            fact=records.string(data,"fact",3000,True)
            source=records.url(records.string(data,"source",2048,True))
            result=db.execute("INSERT INTO evidence(company_id,fact,source) VALUES(?,?,?)",(pk,fact,source)).lastrowid
            audit("evidence.add",{"id":result})
        return {"id":result}

    @app.post("/api/messages/{message_id}")
    def save_message(message_id:int,data:dict):
        with atomic() as db:
            records.entity("messages",message_id)
            result=db.execute("UPDATE messages SET subject=?,body=?,version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND version=?",
                (records.string(data,"subject",300),records.string(data,"body",12000,True),message_id,records.integer(data.get("version"))))
            if not result.rowcount: records.bad("他の編集が先に保存されました。更新後に再編集してください。",409)
            engine.mark_draft_reviewed(message_id)
            audit("message.edit",{"id":message_id})
        return {"id":message_id}

    @app.post("/api/settings")
    def settings(data:dict):
        with atomic() as db:
            fields=("sender_name","sender_email","sender_address","optout")
            for k in fields:
                if k in data:
                    value=records.string(data,k,500)
                    if "\r" in value or "\n" in value: records.bad("改行は使用できません。")
                    db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,value))
            audit("settings.save",{"fields":[k for k in fields if k in data]})
        return {"ok":True}

    @app.get("/api/profile/{company_id}")
    def profile_view(company_id:int):
        from .insights import profile
        records.entity("companies",company_id)
        return profile(company_id)

    @app.get("/api/insights/{campaign_id}")
    def insights_view(campaign_id:int):
        from .insights import feedback
        records.entity("campaigns",campaign_id)
        return feedback(campaign_id)

    @app.post("/api/ai")
    def ai(data:dict):
        from .local_ai import enhance
        return enhance(records.integer(data.get("company_id")),records.integer(data.get("campaign_id")))

    @app.post("/api/engine")
    def act(data:dict):
        action=data.get("action")
        if action=="research": return engine.research_company(records.integer(data.get("company_id")))
        if action=="discover": return engine.discover(records.integer(data.get("campaign_id")))
        if action in ("analyze","generate"):
            co, ca = records.integer(data.get("company_id")), records.integer(data.get("campaign_id"))
            return (engine.analyze if action=="analyze" else engine.generate)(co,ca)
        records.bad("処理を選択してください。")
    return app
