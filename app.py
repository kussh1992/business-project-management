import streamlit as st
import os
import re
import psycopg2
from psycopg2.extras import RealDictCursor
import pandas as pd
from pathlib import Path
from datetime import date, datetime, timedelta
import io
import json

APP_DIR = Path(__file__).resolve().parent
DATABASE_URL = st.secrets.get("DATABASE_URL", os.getenv("DATABASE_URL", ""))
if not DATABASE_URL:
    st.error("Database connection is not configured. Add DATABASE_URL in Streamlit Secrets.")
    st.stop()

TABLES = {"bpm_settings": "bpm_settings", "bpm_projects": "bpm_projects", "bpm_project_steps": "bpm_project_steps", "bpm_daily_logs": "bpm_daily_logs", "bpm_audit_log": "bpm_audit_log"}

class CursorAdapter:
    def __init__(self, cursor): self.cursor = cursor
    def fetchone(self): return self.cursor.fetchone()
    def fetchall(self): return self.cursor.fetchall()
    @property
    def lastrowid(self):
        row = self.cursor.fetchone()
        return row["id"] if row else None

class ConnectionAdapter:
    def __init__(self): self.con = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, connect_timeout=15)
    def __enter__(self): return self
    def __exit__(self, typ, val, tb):
        if typ: self.con.rollback()
        else: self.con.commit()
        self.con.close()
    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s")
        # Preserve SQLite's lastrowid pattern for these inserts.
        if re.match(r"\s*INSERT INTO (bpm_projects|bpm_project_steps|bpm_daily_logs)\b", sql, re.I) and "RETURNING" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
        cur = self.con.cursor()
        cur.execute(sql, params)
        return CursorAdapter(cur)
    def executescript(self, script):
        cur = self.con.cursor()
        for statement in script.split(";"):
            if statement.strip(): cur.execute(statement)
    def close(self): self.con.close()

def connect():
    return ConnectionAdapter()

st.set_page_config(page_title="Business Project Management", page_icon="📊", layout="wide")

DEFAULTS = {
    "categories": ["Automation", "Power BI", "Analytics", "SQL", "Web Application", "Operations"],
    "stages": ["Not Started", "Planning", "In Progress", "On Hold", "Testing", "Completed"],
    "labels": ["Red", "Amber", "Green", "Blocked", "Quick Win"],
    "urgency": ["Low", "Medium", "High", "Critical"],
    "importance": ["Low", "Medium", "High", "Critical"],
    "owners": ["Unassigned"],
}

def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS bpm_settings (
            setting_type TEXT NOT NULL,
            value TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(setting_type, value)
        );
        CREATE TABLE IF NOT EXISTS bpm_projects (
            id BIGSERIAL PRIMARY KEY,
            project_code TEXT UNIQUE,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            category TEXT NOT NULL,
            urgency TEXT NOT NULL,
            importance TEXT NOT NULL,
            stage TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT 'Green',
            owner TEXT NOT NULL DEFAULT 'Unassigned',
            start_date TEXT,
            target_date TEXT,
            completion_date TEXT,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'Admin',
            updated_at TEXT NOT NULL,
            is_archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS bpm_project_steps (
            id BIGSERIAL PRIMARY KEY,
            project_id BIGINT NOT NULL,
            step_name TEXT NOT NULL,
            owner TEXT DEFAULT '',
            planned_start TEXT,
            due_date TEXT,
            status TEXT NOT NULL DEFAULT 'Not Started',
            completed_date TEXT,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES bpm_projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS bpm_daily_logs (
            id BIGSERIAL PRIMARY KEY,
            project_id BIGINT NOT NULL,
            log_date TEXT NOT NULL,
            entered_by TEXT NOT NULL,
            update_text TEXT NOT NULL,
            blockers TEXT DEFAULT '',
            next_action TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES bpm_projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS bpm_audit_log (
            id BIGSERIAL PRIMARY KEY,
            project_id BIGINT,
            entity_type TEXT NOT NULL,
            entity_id BIGINT,
            action TEXT NOT NULL,
            field_name TEXT,
            old_value TEXT,
            new_value TEXT,
            changed_by TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            note TEXT DEFAULT '',
            FOREIGN KEY(project_id) REFERENCES bpm_projects(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bpm_projects_category ON bpm_projects(category);
        CREATE INDEX IF NOT EXISTS idx_bpm_projects_stage ON bpm_projects(stage);
        CREATE INDEX IF NOT EXISTS idx_bpm_logs_project ON bpm_daily_logs(project_id, log_date);
        CREATE INDEX IF NOT EXISTS idx_bpm_audit_project ON bpm_audit_log(project_id, changed_at);
        """)
        for kind, values in DEFAULTS.items():
            for i, value in enumerate(values):
                con.execute(
                    "INSERT INTO bpm_settings(setting_type,value,sort_order,is_active) VALUES(?,?,?,1) ON CONFLICT (setting_type,value) DO NOTHING",
                    (kind, value, i)
                )

def now_text():
    return datetime.now().isoformat(timespec="seconds")

def get_options(kind, include_inactive=False):
    with connect() as con:
        q = "SELECT value FROM bpm_settings WHERE setting_type=?"
        if not include_inactive:
            q += " AND is_active=1"
        q += " ORDER BY sort_order, value"
        rows = con.execute(q, (kind,)).fetchall()
    return [r["value"] for r in rows]

def add_audit(con, project_id, entity_type, entity_id, action, field_name=None,
              old_value=None, new_value=None, changed_by="Admin", note=""):
    con.execute("""INSERT INTO bpm_audit_log
        (project_id,entity_type,entity_id,action,field_name,old_value,new_value,changed_by,changed_at,note)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (project_id, entity_type, entity_id, action, field_name,
         None if old_value is None else str(old_value),
         None if new_value is None else str(new_value),
         changed_by, now_text(), note))

def df_query(sql, params=()):
    with connect() as con:
        cur = con.execute(sql, params)
        rows = cur.fetchall()
        return pd.DataFrame([dict(r) for r in rows])

def get_project(project_id):
    with connect() as con:
        row = con.execute("SELECT * FROM bpm_projects WHERE id=?", (int(project_id),)).fetchone()
        return dict(row) if row else None

def get_steps(project_id):
    return df_query("SELECT * FROM bpm_project_steps WHERE project_id=? ORDER BY COALESCE(due_date,'9999-12-31'), id", (int(project_id),))

def project_title(row):
    return f"{row['project_code']} — {row['name']}" if row.get("project_code") else row["name"]

def fetch_projects(include_archived=False):
    sql = """SELECT p.*,
        (SELECT COUNT(*) FROM bpm_project_steps s WHERE s.project_id=p.id) AS total_steps,
        (SELECT COUNT(*) FROM bpm_project_steps s WHERE s.project_id=p.id AND s.status='Completed') AS completed_steps
        FROM bpm_projects p"""
    if not include_archived:
        sql += " WHERE p.is_archived=0"
    sql += " ORDER BY CASE lower(p.label) WHEN 'red' THEN 0 WHEN 'critical' THEN 0 WHEN 'amber' THEN 1 ELSE 2 END, p.target_date IS NULL, p.target_date, p.id DESC"
    return df_query(sql)

def save_setting(kind, value):
    value = value.strip()
    if not value:
        return False, "Value cannot be blank."
    with connect() as con:
        exists = con.execute("SELECT 1 FROM bpm_settings WHERE setting_type=? AND lower(value)=lower(?)", (kind, value)).fetchone()
        if exists:
            return False, "That option already exists."
        order = con.execute("SELECT COALESCE(MAX(sort_order),-1)+1 AS n FROM bpm_settings WHERE setting_type=?", (kind,)).fetchone()["n"]
        con.execute("INSERT INTO bpm_settings(setting_type,value,sort_order,is_active) VALUES(?,?,?,1)", (kind, value, order))
    return True, f"Added {value}."

def deactivate_setting(kind, value):
    with connect() as con:
        con.execute("UPDATE bpm_settings SET is_active=0 WHERE setting_type=? AND value=?", (kind, value))

def audit_changes(con, project_id, before, after, changed_by, note=""):
    fields = ["name","description","category","urgency","importance","stage","label","owner",
              "start_date","target_date","completion_date"]
    for field in fields:
        old, new = before.get(field), after.get(field)
        if ("" if old is None else str(old)) != ("" if new is None else str(new)):
            add_audit(con, project_id, "project", project_id, "UPDATE", field, old, new, changed_by, note)

def save_project(project_id, data, changed_by, note=""):
    stamp = now_text()
    with connect() as con:
        if project_id:
            before_row = con.execute("SELECT * FROM bpm_projects WHERE id=?", (project_id,)).fetchone()
            if not before_row:
                raise ValueError("Project not found.")
            before = dict(before_row)
            con.execute("""UPDATE bpm_projects SET name=?, description=?, category=?, urgency=?, importance=?,
                stage=?, label=?, owner=?, start_date=?, target_date=?, completion_date=?, updated_at=?
                WHERE id=?""",
                (data["name"], data["description"], data["category"], data["urgency"], data["importance"],
                 data["stage"], data["label"], data["owner"], data["start_date"], data["target_date"],
                 data["completion_date"], stamp, project_id))
            after = dict(data)
            audit_changes(con, project_id, before, after, changed_by, note)
            add_audit(con, project_id, "project", project_id, "PROJECT_SAVED", changed_by=changed_by, note=note or "Project details submitted")
            return project_id
        code = data.get("project_code") or f"PRJ-{datetime.now():%y%m%d}-{int(datetime.now().timestamp())%100000:05d}"
        cur = con.execute("""INSERT INTO bpm_projects
            (project_code,name,description,category,urgency,importance,stage,label,owner,start_date,target_date,
             completion_date,created_at,created_by,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, data["name"], data["description"], data["category"], data["urgency"], data["importance"],
             data["stage"], data["label"], data["owner"], data["start_date"], data["target_date"],
             data["completion_date"], stamp, changed_by, stamp))
        pid = cur.lastrowid
        add_audit(con, pid, "project", pid, "CREATE", changed_by=changed_by, note=f"Project created: {data['name']}")
        return pid

def save_steps(project_id, rows, changed_by):
    stamp = now_text()
    with connect() as con:
        existing = {r["id"]: dict(r) for r in con.execute("SELECT * FROM bpm_project_steps WHERE project_id=?", (project_id,)).fetchall()}
        submitted_ids = set()
        for row in rows:
            step_name = str(row.get("step_name") or "").strip()
            if not step_name:
                continue
            sid = row.get("id")
            fields = (step_name, str(row.get("owner") or ""), row.get("planned_start") or None,
                      row.get("due_date") or None, str(row.get("status") or "Not Started"),
                      row.get("completed_date") or None, str(row.get("notes") or ""))
            if sid and int(sid) in existing:
                sid = int(sid); submitted_ids.add(sid)
                old = existing[sid]
                old_vals = [old["step_name"], old["owner"], old["planned_start"], old["due_date"], old["status"], old["completed_date"], old["notes"]]
                if [None if x is None else str(x) for x in old_vals] != [None if x is None else str(x) for x in fields]:
                    con.execute("""UPDATE bpm_project_steps SET step_name=?,owner=?,planned_start=?,due_date=?,status=?,
                        completed_date=?,notes=?,updated_at=? WHERE id=?""", (*fields, stamp, sid))
                    add_audit(con, project_id, "step", sid, "UPDATE", "step_details", json.dumps(old_vals), json.dumps(fields), changed_by)
            else:
                cur = con.execute("""INSERT INTO bpm_project_steps
                    (project_id,step_name,owner,planned_start,due_date,status,completed_date,notes,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (project_id, *fields, stamp, stamp))
                sid = cur.lastrowid; submitted_ids.add(sid)
                add_audit(con, project_id, "step", sid, "CREATE", "step_name", None, step_name, changed_by)
        # Existing steps omitted from the editor are retained intentionally to avoid accidental deletion.
        con.execute("UPDATE bpm_projects SET updated_at=? WHERE id=?", (stamp, project_id))

def add_daily_log(project_id, log_date, entered_by, update_text, blockers, next_action, changed_by):
    if not update_text.strip():
        raise ValueError("Please enter a daily update.")
    with connect() as con:
        cur = con.execute("""INSERT INTO bpm_daily_logs(project_id,log_date,entered_by,update_text,blockers,next_action,created_at)
            VALUES(?,?,?,?,?,?,?)""", (project_id, str(log_date), entered_by, update_text.strip(), blockers.strip(), next_action.strip(), now_text()))
        log_id = cur.lastrowid
        add_audit(con, project_id, "daily_log", log_id, "CREATE", "daily_update", None, update_text.strip(), changed_by,
                  f"Daily log for {log_date}; blockers: {blockers.strip()}")
        return log_id

def safe_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None

def date_input_value(value):
    d = safe_date(value)
    return d if d else date.today()

def sidebar_user():
    st.sidebar.title("📊 Project Hub")
    st.sidebar.caption("Business Project Management")
    user = st.sidebar.text_input("Your name (audit attribution)", value=st.session_state.get("audit_user", "Admin"), key="audit_user_input")
    if user.strip():
        st.session_state["audit_user"] = user.strip()
    st.sidebar.caption("Database: Supabase PostgreSQL")
    st.sidebar.info("Project data and audit history are stored in the shared Supabase database.")
    return user.strip() or "Admin"

init_db()
user = sidebar_user()

st.title("Business Project Management")
st.caption("Configurable project tracking • daily logs • step timelines • audit trail")

pages = [
    "Dashboard",
    "All Projects",
    "Project by Category",
    "Report Exports",
    "Control Panel",
    "Activity & Audit History",
]
page = st.sidebar.radio("Navigate", pages)

bpm_projects = fetch_projects()
categories = get_options("categories")
stages = get_options("stages")
labels = get_options("labels")
urgencies = get_options("urgency")
importances = get_options("importance")
owners = get_options("owners")

def stage_color(stage):
    return {"Completed":"🟢","In Progress":"🔵","On Hold":"🟠","Blocked":"🔴","Not Started":"⚪","Planning":"🟣","Testing":"🧪"}.get(stage, "•")

def show_project_table(df, columns=None):
    if df.empty:
        st.info("No bpm_projects match this view.")
        return
    view = df.copy()
    if "target_date" in view.columns:
        view["target_date"] = view["target_date"].fillna("")
    if "completion_pct" not in view.columns and "total_steps" in view.columns:
        view["completion_pct"] = view.apply(lambda r: round(100*r["completed_steps"]/r["total_steps"]) if r["total_steps"] else (100 if r["stage"]=="Completed" else 0), axis=1)
    cols = columns or [c for c in ["project_code","name","category","label","urgency","importance","stage","owner","start_date","target_date","completion_pct"] if c in view.columns]
    st.dataframe(view[cols], use_container_width=True, hide_index=True)

if page == "Dashboard":
    st.header("Dashboard")
    today = date.today()
    active = bpm_projects[bpm_projects["stage"] != "Completed"] if not bpm_projects.empty else bpm_projects
    completed = bpm_projects[bpm_projects["stage"] == "Completed"] if not bpm_projects.empty else bpm_projects
    overdue = bpm_projects[(bpm_projects["target_date"].notna()) & (bpm_projects["target_date"] < today.isoformat()) & (bpm_projects["stage"] != "Completed")] if not bpm_projects.empty else bpm_projects
    critical = bpm_projects[(bpm_projects["urgency"].str.lower() == "critical") | (bpm_projects["label"].str.lower() == "red")] if not bpm_projects.empty else bpm_projects
    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Total bpm_projects", len(bpm_projects))
    c2.metric("Active", len(active))
    c3.metric("Completed", len(completed))
    c4.metric("Overdue", len(overdue))
    c5.metric("Red / Critical", len(critical))
    st.divider()
    left, right = st.columns(2)
    with left:
        st.subheader("Projects by stage")
        if bpm_projects.empty:
            st.info("Create your first project on All Projects.")
        else:
            stage_counts = bpm_projects.groupby("stage").size().rename("Projects").reset_index()
            st.bar_chart(stage_counts.set_index("stage"))
    with right:
        st.subheader("Projects by category")
        if bpm_projects.empty:
            st.info("Category chart will appear when bpm_projects are added.")
        else:
            cat_counts = bpm_projects.groupby("category").size().rename("Projects").reset_index()
            st.bar_chart(cat_counts.set_index("category"))
    st.subheader("Priority queue")
    show_project_table(bpm_projects.head(10))
    st.subheader("Upcoming deadlines (next 14 days)")
    if not bpm_projects.empty:
        upcoming = bpm_projects[(bpm_projects["target_date"].notna()) & (bpm_projects["target_date"] >= today.isoformat()) &
                            (bpm_projects["target_date"] <= (today+timedelta(days=14)).isoformat()) &
                            (bpm_projects["stage"] != "Completed")]
        show_project_table(upcoming)
    with st.expander("Recent activity"):
        recent = df_query("SELECT changed_at,changed_by,action,field_name,old_value,new_value,note,project_id FROM bpm_audit_log ORDER BY id DESC LIMIT 12")
        st.dataframe(recent, use_container_width=True, hide_index=True)

elif page == "All Projects":
    st.header("All Projects")
    with st.expander("➕ Create a new project", expanded=bpm_projects.empty):
        with st.form("create_project_form", clear_on_submit=True):
            col1,col2,col3 = st.columns(3)
            name = col1.text_input("Project name *")
            category = col2.selectbox("Category", categories or ["General"])
            owner = col3.selectbox("Owner", owners or ["Unassigned"])
            description = st.text_area("Description / objective")
            col4,col5,col6,col7 = st.columns(4)
            urgency = col4.selectbox("Urgency", urgencies or ["Medium"], index=(urgencies.index("High") if "High" in urgencies else 0))
            importance = col5.selectbox("Importance", importances or ["Medium"], index=(importances.index("High") if "High" in importances else 0))
            stage = col6.selectbox("Stage", stages or ["Not Started"])
            label = col7.selectbox("Label", labels or ["Green"], index=(next((i for i,x in enumerate(labels) if x.lower()=="red"),0)))
            col8,col9 = st.columns(2)
            start_date = col8.date_input("Start date", value=date.today())
            target_date = col9.date_input("Target date", value=date.today()+timedelta(days=14))
            submitted = st.form_submit_button("Create project", type="primary")
        if submitted:
            if not name.strip():
                st.error("Project name is required.")
            else:
                data = {"name":name.strip(),"description":description,"category":category,"urgency":urgency,"importance":importance,
                        "stage":stage,"label":label,"owner":owner,"start_date":str(start_date),"target_date":str(target_date),
                        "completion_date":str(date.today()) if stage=="Completed" else None}
                try:
                    pid = save_project(None, data, user)
                    st.success(f"Created project #{pid}.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not create project: {e}")

    if not bpm_projects.empty:
        st.subheader("Category / stage overview")
        overview = bpm_projects.groupby(["category","stage"]).size().rename("Projects").reset_index()
        for cat in categories:
            catdf = overview[overview["category"] == cat]
            if not catdf.empty:
                with st.expander(f"📁 {cat}  ·  {int(catdf['Projects'].sum())} bpm_projects"):
                    st.dataframe(catdf, use_container_width=True, hide_index=True)
        st.subheader("Search and filter")
        f1,f2,f3,f4 = st.columns(4)
        search = f1.text_input("Search name / code")
        fc = f2.multiselect("Category", categories, default=[])
        fs = f3.multiselect("Stage", stages, default=[])
        fl = f4.multiselect("Label", labels, default=[])
        f5,f6,f7 = st.columns(3)
        fu = f5.multiselect("Urgency", urgencies, default=[])
        fi = f6.multiselect("Importance", importances, default=[])
        fo = f7.multiselect("Owner", owners, default=[])
        filtered = bpm_projects.copy()
        if search:
            filtered = filtered[filtered["name"].str.contains(search, case=False, na=False) | filtered["project_code"].str.contains(search, case=False, na=False)]
        for col, vals in [("category",fc),("stage",fs),("label",fl),("urgency",fu),("importance",fi),("owner",fo)]:
            if vals:
                filtered = filtered[filtered[col].isin(vals)]
        show_project_table(filtered)
        st.subheader("Project details, steps and history")
        opts = {project_title(r): int(r["id"]) for _,r in filtered.iterrows()}
        if opts:
            selected_title = st.selectbox("Select a project", list(opts.keys()), key="all_project_select")
            pid = opts[selected_title]
            p = get_project(pid)
            with st.expander("Edit project details", expanded=False):
                with st.form(f"edit_project_{pid}"):
                    a,b,c = st.columns(3)
                    e_name = a.text_input("Project name", value=p["name"])
                    e_category = b.selectbox("Category", categories, index=categories.index(p["category"]) if p["category"] in categories else 0)
                    e_owner = c.selectbox("Owner", owners, index=owners.index(p["owner"]) if p["owner"] in owners else 0)
                    e_desc = st.text_area("Description", value=p["description"] or "")
                    d,e,f,g = st.columns(4)
                    e_urgency = d.selectbox("Urgency", urgencies, index=urgencies.index(p["urgency"]) if p["urgency"] in urgencies else 0)
                    e_importance = e.selectbox("Importance", importances, index=importances.index(p["importance"]) if p["importance"] in importances else 0)
                    e_stage = f.selectbox("Stage", stages, index=stages.index(p["stage"]) if p["stage"] in stages else 0)
                    e_label = g.selectbox("Label", labels, index=labels.index(p["label"]) if p["label"] in labels else 0)
                    h,i = st.columns(2)
                    e_start = h.date_input("Start date", value=date_input_value(p["start_date"]), key=f"start_{pid}")
                    e_target = i.date_input("Target date", value=date_input_value(p["target_date"]), key=f"target_{pid}")
                    e_completion = st.date_input("Completion date (only if completed)", value=date_input_value(p["completion_date"]) if p["completion_date"] else date.today(), key=f"complete_{pid}")
                    edit_note = st.text_input("Change note (optional)")
                    save = st.form_submit_button("Save project changes", type="primary")
                if save:
                    comp = str(e_completion) if e_stage == "Completed" else None
                    data = {"name":e_name.strip(),"description":e_desc,"category":e_category,"urgency":e_urgency,
                            "importance":e_importance,"stage":e_stage,"label":e_label,"owner":e_owner,
                            "start_date":str(e_start),"target_date":str(e_target),"completion_date":comp}
                    if not e_name.strip():
                        st.error("Project name is required.")
                    else:
                        save_project(pid, data, user, edit_note)
                        st.success("Changes saved and audit history recorded.")
                        st.rerun()
            with st.expander("Manage project steps", expanded=True):
                st.caption("Add rows for milestones/steps. Existing steps are retained if their row is left blank.")
                old_steps = get_steps(pid)
                if not old_steps.empty:
                    st.dataframe(old_steps[["step_name","owner","planned_start","due_date","status","completed_date","notes"]], use_container_width=True, hide_index=True)
                n_new = st.number_input("New step rows to add", min_value=1, max_value=10, value=3, key=f"step_count_{pid}")
                with st.form(f"steps_form_{pid}"):
                    step_rows = []
                    for i in range(int(n_new)):
                        st.markdown(f"**New step {i+1}**")
                        x1,x2,x3,x4 = st.columns(4)
                        sn = x1.text_input("Step name", key=f"sn_{pid}_{i}")
                        so = x2.selectbox("Step owner", owners or ["Unassigned"], key=f"so_{pid}_{i}")
                        sd = x3.date_input("Due date", value=date.today()+timedelta(days=i+1), key=f"sd_{pid}_{i}")
                        ss = x4.selectbox("Status", stages or ["Not Started"], key=f"ss_{pid}_{i}")
                        step_rows.append({"step_name":sn,"owner":so,"due_date":str(sd),"status":ss,
                                          "planned_start":str(date.today()),"completed_date":str(date.today()) if ss=="Completed" else None,"notes":""})
                    steps_submit = st.form_submit_button("Save new steps")
                if steps_submit:
                    save_steps(pid, step_rows, user)
                    st.success("Steps saved.")
                    st.rerun()
            with st.expander("Project daily logs"):
                logs = df_query("SELECT log_date,entered_by,update_text,blockers,next_action,created_at FROM bpm_daily_logs WHERE project_id=? ORDER BY log_date DESC,id DESC", (pid,))
                st.dataframe(logs, use_container_width=True, hide_index=True)
            with st.expander("Project audit log"):
                aud = df_query("SELECT changed_at,changed_by,action,field_name,old_value,new_value,note FROM bpm_audit_log WHERE project_id=? ORDER BY id DESC", (pid,))
                st.dataframe(aud, use_container_width=True, hide_index=True)
    else:
        st.info("No bpm_projects yet. Use the form above to create your first project.")

elif page == "Project by Category":
    st.header("Project by Category")
    st.caption("Expand a category to enter daily updates. Red labels are shown first within each category.")
    if bpm_projects.empty:
        st.info("Create bpm_projects in All Projects first.")
    for cat in categories:
        catdf = bpm_projects[bpm_projects["category"] == cat].copy()
        catdf["_red"] = catdf["label"].str.lower().eq("red").map({True:0,False:1})
        catdf = catdf.sort_values(["_red","target_date","name"], na_position="last")
        with st.expander(f"📂 {cat} · {len(catdf)} project(s)", expanded=False):
            if catdf.empty:
                st.caption("No bpm_projects in this category yet.")
                continue
            for _, r in catdf.iterrows():
                pid = int(r["id"])
                urgency_icon = "🔴" if str(r["urgency"]).lower()=="critical" else ("🟠" if str(r["urgency"]).lower()=="high" else "⚪")
                with st.container(border=True):
                    st.markdown(f"**{r['label'].upper()} · {r['name']}**  {urgency_icon}")
                    st.caption(f"{r['project_code']} · Stage: {r['stage']} · Owner: {r['owner']} · Due: {r['target_date'] or 'Not set'}")
                    with st.form(f"daily_{pid}"):
                        l1,l2 = st.columns([1,2])
                        log_date = l1.date_input("Log date", value=date.today(), key=f"log_date_{pid}")
                        entered_by = l2.text_input("Entered by", value=user, key=f"log_user_{pid}")
                        update_text = st.text_area("Daily update *", placeholder="What changed or progressed today?", key=f"log_update_{pid}")
                        l3,l4 = st.columns(2)
                        blockers = l3.text_area("Blockers / risks", key=f"log_blockers_{pid}")
                        next_action = l4.text_area("Next action", key=f"log_next_{pid}")
                        st.markdown("**Optional project-field changes (saved with this submission)**")
                        q1,q2,q3,q4 = st.columns(4)
                        new_stage = q1.selectbox("Stage", stages, index=stages.index(r["stage"]) if r["stage"] in stages else 0, key=f"daily_stage_{pid}")
                        new_urgency = q2.selectbox("Urgency", urgencies, index=urgencies.index(r["urgency"]) if r["urgency"] in urgencies else 0, key=f"daily_urgency_{pid}")
                        new_importance = q3.selectbox("Importance", importances, index=importances.index(r["importance"]) if r["importance"] in importances else 0, key=f"daily_importance_{pid}")
                        new_label = q4.selectbox("Label", labels, index=labels.index(r["label"]) if r["label"] in labels else 0, key=f"daily_label_{pid}")
                        submit_log = st.form_submit_button("Submit daily log + changes", type="primary")
                    if submit_log:
                        try:
                            with connect() as con:
                                add_daily_log(pid, log_date, entered_by.strip() or user, update_text, blockers, next_action, user)
                                before = dict(con.execute("SELECT * FROM bpm_projects WHERE id=?", (pid,)).fetchone())
                                after = before.copy()
                                after.update({"stage":new_stage,"urgency":new_urgency,"importance":new_importance,"label":new_label})
                                for field in ["stage","urgency","importance","label"]:
                                    if str(before[field]) != str(after[field]):
                                        con.execute(f"UPDATE bpm_projects SET {field}=?,updated_at=? WHERE id=?", (after[field],now_text(),pid))
                                        add_audit(con,pid,"project",pid,"UPDATE",field,before[field],after[field],user,"Changed via daily log submission")
                                if new_stage == "Completed" and not before.get("completion_date"):
                                    con.execute("UPDATE bpm_projects SET completion_date=? WHERE id=?", (str(date.today()),pid))
                                    add_audit(con,pid,"project",pid,"UPDATE","completion_date",None,str(date.today()),user,"Set on completion")
                            st.success("Daily log submitted; project history and changed fields were recorded.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not submit log: {e}")
                    with st.expander("Recent logs and change history"):
                        st.markdown("**Daily logs**")
                        ldf = df_query("SELECT log_date,entered_by,update_text,blockers,next_action FROM bpm_daily_logs WHERE project_id=? ORDER BY log_date DESC,id DESC LIMIT 10", (pid,))
                        st.dataframe(ldf, use_container_width=True, hide_index=True)
                        st.markdown("**Audit history**")
                        adf = df_query("SELECT changed_at,changed_by,action,field_name,old_value,new_value,note FROM bpm_audit_log WHERE project_id=? ORDER BY id DESC LIMIT 15", (pid,))
                        st.dataframe(adf, use_container_width=True, hide_index=True)

elif page == "Report Exports":
    st.header("Report Exports")
    st.caption("Apply filters, preview the result, then download CSV or Excel workbooks.")
    allp = fetch_projects(include_archived=True)
    f1,f2,f3 = st.columns(3)
    cat_filter = f1.multiselect("Category", get_options("categories", True))
    stage_filter = f2.multiselect("Stage", get_options("stages", True))
    label_filter = f3.multiselect("Label", get_options("labels", True))
    f4,f5,f6 = st.columns(3)
    urgency_filter = f4.multiselect("Urgency", get_options("urgency", True))
    importance_filter = f5.multiselect("Importance", get_options("importance", True))
    owner_filter = f6.multiselect("Owner", get_options("owners", True))
    f7,f8,f9 = st.columns(3)
    from_date = f7.date_input("Start date from", value=date.today()-timedelta(days=365))
    to_date = f8.date_input("Start date to", value=date.today()+timedelta(days=365))
    include_archived = f9.checkbox("Include archived", value=True)
    search = st.text_input("Search project name, code or description")
    filtered = allp.copy()
    if not include_archived:
        filtered = filtered[filtered["is_archived"]==0]
    for col, vals in [("category",cat_filter),("stage",stage_filter),("label",label_filter),("urgency",urgency_filter),("importance",importance_filter),("owner",owner_filter)]:
        if vals:
            filtered = filtered[filtered[col].isin(vals)]
    if from_date and to_date:
        filtered = filtered[(filtered["start_date"].fillna("") >= str(from_date)) & (filtered["start_date"].fillna("") <= str(to_date))]
    if search:
        mask = filtered["name"].str.contains(search, case=False, na=False) | filtered["project_code"].str.contains(search, case=False, na=False) | filtered["description"].str.contains(search, case=False, na=False)
        filtered = filtered[mask]
    st.metric("Projects in export", len(filtered))
    show_project_table(filtered)
    ids = [int(x) for x in filtered["id"].tolist()] if not filtered.empty else []
    if ids:
        marks = ",".join("?" for _ in ids)
        steps_export = df_query(f"SELECT s.*,p.project_code,p.name AS project_name,p.category,p.stage AS project_stage FROM bpm_project_steps s JOIN bpm_projects p ON p.id=s.project_id WHERE p.id IN ({marks}) ORDER BY p.id,s.id", ids)
        logs_export = df_query(f"SELECT l.*,p.project_code,p.name AS project_name,p.category FROM bpm_daily_logs l JOIN bpm_projects p ON p.id=l.project_id WHERE p.id IN ({marks}) ORDER BY l.log_date DESC,l.id DESC", ids)
        audit_export = df_query(f"SELECT a.*,p.project_code,p.name AS project_name FROM bpm_audit_log a LEFT JOIN bpm_projects p ON p.id=a.project_id WHERE a.project_id IN ({marks}) ORDER BY a.changed_at DESC,a.id DESC", ids)
    else:
        steps_export = pd.DataFrame()
        logs_export = pd.DataFrame()
        audit_export = pd.DataFrame()
    csv_bytes = filtered.to_csv(index=False).encode("utf-8-sig")
    st.download_button("Download Projects CSV", data=csv_bytes, file_name="projects_export.csv", mime="text/csv")
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        filtered.to_excel(writer, sheet_name="Projects", index=False)
        steps_export.to_excel(writer, sheet_name="Steps", index=False)
        logs_export.to_excel(writer, sheet_name="Daily Logs", index=False)
        audit_export.to_excel(writer, sheet_name="Audit History", index=False)
    st.download_button("Download full Excel workbook", data=output.getvalue(), file_name="project_management_export.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.caption("The Excel workbook contains Projects, Steps, Daily Logs and Audit History sheets.")

elif page == "Control Panel":
    st.header("Control Panel")
    st.caption("Configure dropdowns used throughout the application. Deactivated options remain in historical records.")
    control_types = {
        "Categories": "categories",
        "Stages": "stages",
        "Labels": "labels",
        "Urgency levels": "urgency",
        "Importance levels": "importance",
        "Project owners": "owners",
    }
    for title, kind in control_types.items():
        with st.expander(title, expanded=(kind=="categories")):
            active = get_options(kind)
            inactive = get_options(kind, include_inactive=True)
            st.write("Active options: " + (", ".join(active) if active else "None"))
            with st.form(f"add_setting_{kind}"):
                new_value = st.text_input(f"Add {title.lower().rstrip('s')}", key=f"new_setting_{kind}")
                add_clicked = st.form_submit_button("Add option")
            if add_clicked:
                ok, msg = save_setting(kind, new_value)
                if ok: st.success(msg); st.rerun()
                else: st.warning(msg)
            if active:
                selected = st.selectbox(f"Deactivate an option in {title.lower()}", active, key=f"deact_select_{kind}")
                if st.button(f"Deactivate selected {title.lower().rstrip('s')}", key=f"deact_btn_{kind}"):
                    if len(active) == 1:
                        st.error("Keep at least one active option.")
                    else:
                        deactivate_setting(kind, selected)
                        st.success(f"Deactivated {selected}. Existing records are preserved.")
                        st.rerun()
            with st.expander("Show inactive options"):
                inactive_values = [v for v in inactive if v not in active]
                st.write(", ".join(inactive_values) if inactive_values else "No inactive options.")
    st.divider()
    st.subheader("Database status")
    st.caption("Data is stored in Supabase PostgreSQL and persists across app restarts.")
    with connect() as con:
        counts = {table: con.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                  for table in ["bpm_projects","bpm_project_steps","bpm_daily_logs","bpm_audit_log","bpm_settings"]}
    st.json(counts)

elif page == "Activity & Audit History":
    st.header("Activity & Audit History")
    audit = df_query("""SELECT a.id,a.changed_at,a.changed_by,a.action,a.entity_type,a.entity_id,
        a.field_name,a.old_value,a.new_value,a.note,a.project_id,p.project_code,p.name AS project_name
        FROM bpm_audit_log a LEFT JOIN bpm_projects p ON p.id=a.project_id ORDER BY a.id DESC""")
    if audit.empty:
        st.info("Audit history will appear after bpm_projects, steps or daily logs are created.")
    else:
        a1,a2,a3 = st.columns(3)
        search = a1.text_input("Search project / field / note")
        action_opts = sorted(audit["action"].dropna().unique().tolist())
        actions = a2.multiselect("Action", action_opts)
        user_opts = sorted(audit["changed_by"].dropna().unique().tolist())
        users = a3.multiselect("Changed by", user_opts)
        if search:
            mask = audit.astype(str).apply(lambda col: col.str.contains(search, case=False, na=False)).any(axis=1)
            audit = audit[mask]
        if actions:
            audit = audit[audit["action"].isin(actions)]
        if users:
            audit = audit[audit["changed_by"].isin(users)]
        st.dataframe(audit, use_container_width=True, hide_index=True)
        st.download_button("Download audit CSV", audit.to_csv(index=False).encode("utf-8-sig"),
                           "audit_history.csv", "text/csv")

st.sidebar.divider()
st.sidebar.caption("Tip: use Control Panel to customize dropdowns before adding many bpm_projects.")
