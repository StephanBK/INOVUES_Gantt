"""
INOVUES Project Gantt — Cross-project Gantt chart with two-way Odoo sync.
- Streamlit loads tasks from Odoo → renders DHTMLX Gantt via HTML component
- Drag/resize/link → URL param redirect → Streamlit writes back to Odoo

Improvements:
  1. Project filter sidebar (multiselect show/hide)
  2. Today marker (red vertical line)
  3. Status colors: Approved = green, Canceled = faded/strikethrough
  4. Manual refresh button (clears cache on demand)
  + Export PNG button (downloads full Gantt as image)
"""

import streamlit as st
import streamlit.components.v1 as components
import json
import xmlrpc.client
import os
from datetime import datetime, timedelta

st.set_page_config(page_title="INOVUES Gantt", layout="wide", page_icon="📊")

# ─── Odoo connection ───────────────────────────────────────────
ODOO_URL  = os.environ.get("ODOO_URL",  "https://inovues.odoo.com")
ODOO_DB   = os.environ.get("ODOO_DB",   "inovues")
ODOO_USER = os.environ.get("ODOO_USER", "sketterer@inovues.com")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")


@st.cache_data(ttl=3600)
def get_uid():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        raise Exception("Authentication failed — check ODOO_API_KEY env var.")
    return uid


def odoo_rpc(model, method, args=None, kwargs=None):
    uid = get_uid()
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
                             model, method, args or [], kwargs or {})


# ─── Process write-back actions BEFORE rendering ───────────────
params = st.query_params
action = params.get("gantt_action", None)
write_status = None

if action == "update_task":
    task_id = int(params.get("tid", 0))
    start   = params.get("s", "")
    end     = params.get("e", "")
    if task_id and start and end:
        try:
            odoo_rpc("project.task", "write",
                     [[task_id], {
                         "planned_date_begin": f"{start} 08:00:00",
                         "date_deadline":      f"{end} 17:00:00"
                     }])
            write_status = ("success", f"Updated task #{task_id}: {start} → {end}")
            st.cache_data.clear()
        except Exception as e:
            write_status = ("error", f"Failed: {e}")
    st.query_params.clear()

elif action == "add_link":
    source = int(params.get("src", 0))
    target = int(params.get("tgt", 0))
    if source and target:
        try:
            odoo_rpc("project.task", "write",
                     [[target], {"depend_on_ids": [(4, source)]}])
            write_status = ("success", f"Dependency added: #{source} → #{target}")
            st.cache_data.clear()
        except Exception as e:
            write_status = ("error", f"Failed: {e}")
    st.query_params.clear()

elif action == "delete_link":
    source = int(params.get("src", 0))
    target = int(params.get("tgt", 0))
    if source and target:
        try:
            odoo_rpc("project.task", "write",
                     [[target], {"depend_on_ids": [(3, source)]}])
            write_status = ("success", "Dependency removed")
            st.cache_data.clear()
        except Exception as e:
            write_status = ("error", f"Failed: {e}")
    st.query_params.clear()


# ─── Load data ─────────────────────────────────────────────────
@st.cache_data(ttl=60)
def load_projects():
    return odoo_rpc("project.project", "search_read", [[]],
                    {"fields": ["name", "task_count"], "order": "name asc"})


@st.cache_data(ttl=60)
def load_tasks():
    return odoo_rpc("project.task", "search_read", [[]],
                    {"fields": ["name", "project_id", "date_deadline",
                                "planned_date_begin", "stage_id",
                                "depend_on_ids", "state", "priority",
                                "user_ids", "sequence"],
                     "order": "project_id, sequence", "limit": 1000})


# ─── Colors ────────────────────────────────────────────────────
PROJECT_COLORS = [
    "#2E86AB", "#A23B72", "#F18F01", "#C73E1D",
    "#3B1F2B", "#44BBA4", "#E94F37", "#393E41"
]

COLOR_APPROVED = "#27ae60"   # green  — stage == "Approved"
COLOR_CANCELED = "#999999"   # gray   — state == "1_canceled"


def build_gantt_data(projects, tasks, selected_project_ids):
    gantt_data    = []
    gantt_links   = []
    color_map     = {}
    missing_dates = []

    for i, proj in enumerate(projects):
        pid = proj["id"]
        color_map[pid] = PROJECT_COLORS[i % len(PROJECT_COLORS)]

        if pid not in selected_project_ids:
            continue

        gantt_data.append({
            "id":    f"p_{pid}",
            "text":  proj["name"],
            "type":  "project",
            "open":  True,
            "color": color_map[pid],
        })

    for task in tasks:
        if not task["project_id"]:
            continue
        proj_id = task["project_id"][0]

        if proj_id not in selected_project_ids:
            continue

        start = task.get("planned_date_begin")
        end   = task.get("date_deadline")

        if not start and not end:
            missing_dates.append(task)
            continue

        if start and not end:
            end_dt = datetime.strptime(start[:10], "%Y-%m-%d") + timedelta(days=1)
            end = end_dt.strftime("%Y-%m-%d")
        elif end and not start:
            start_dt = datetime.strptime(end[:10], "%Y-%m-%d") - timedelta(days=1)
            start = start_dt.strftime("%Y-%m-%d")

        start_str = start[:10]
        end_str   = end[:10]
        try:
            dur = max((datetime.strptime(end_str,   "%Y-%m-%d") -
                       datetime.strptime(start_str, "%Y-%m-%d")).days, 1)
        except (ValueError, TypeError):
            dur = 1

        stage       = task["stage_id"][1] if task["stage_id"] else ""
        task_state  = task.get("state", "")
        is_canceled = (task_state == "1_canceled")
        is_approved = (stage.strip().lower() == "approved")

        if is_canceled:
            bar_color = COLOR_CANCELED
        elif is_approved:
            bar_color = COLOR_APPROVED
        else:
            bar_color = color_map.get(proj_id, "#999")

        gantt_data.append({
            "id":          task["id"],
            "text":        task["name"],
            "start_date":  start_str,
            "duration":    dur,
            "parent":      f"p_{proj_id}",
            "color":       bar_color,
            "stage":       stage,
            "state":       task_state,
            "is_approved": is_approved,
            "is_canceled": is_canceled,
            "odoo_id":     task["id"],
        })

        for dep_id in task.get("depend_on_ids", []):
            gantt_links.append({
                "id":     f"L{dep_id}_{task['id']}",
                "source": dep_id,
                "target": task["id"],
                "type":   "0",
            })

    return gantt_data, gantt_links, missing_dates, color_map


def build_gantt_html(gantt_data, gantt_links, color_map, projects, selected_project_ids):
    legend_html = "".join([
        f'<div class="lg-i">'
        f'<div class="lg-d" style="background:{color_map.get(p["id"],"#999")}"></div>'
        f'<span>{p["name"]}</span></div>'
        for p in projects
        if p.get("task_count", 0) > 0 and p["id"] in selected_project_ids
    ])
    legend_html += (
        f'<div class="lg-i"><div class="lg-d" style="background:{COLOR_APPROVED}"></div>'
        f'<span>Approved</span></div>'
        f'<div class="lg-i"><div class="lg-d" style="background:{COLOR_CANCELED}"></div>'
        f'<span>Canceled</span></div>'
    )

    today_str = datetime.now().strftime("%Y-%m-%d")
    data_json = json.dumps({"data": gantt_data, "links": gantt_links})

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script src="https://cdn.dhtmlx.com/gantt/edge/dhtmlxgantt.js"></script>
<link  rel="stylesheet" href="https://cdn.dhtmlx.com/gantt/edge/dhtmlxgantt.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<style>
html,body{{margin:0;padding:0;height:100%;overflow:hidden;font-family:'Segoe UI',system-ui,sans-serif}}
#gantt_here{{width:100%;height:calc(100vh - 44px)}}
.project_row{{font-weight:700;background:#f0f0f0!important}}
.project_row .gantt_cell{{font-weight:700}}
.gantt_task_content{{font-size:11px;font-weight:500;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.3)}}
.canceled .gantt_task_content{{text-decoration:line-through}}
.canceled{{opacity:.5}}
.approved .gantt_task_content{{font-weight:700}}
.gantt_link_arrow{{border-color:#e74c3c!important}}
.gantt_line_wrapper div{{background-color:#e74c3c!important}}
.gantt_marker{{background:rgba(220,20,60,0.12);border-left:2px solid #DC143C}}
.gantt_marker_content{{background:#DC143C;color:#fff;font-size:10px;
    padding:2px 5px;border-radius:0 3px 3px 0;white-space:nowrap}}
#tb{{background:#1a1a2e;color:#fff;padding:6px 16px;display:flex;align-items:center;gap:10px;
    font-size:13px;height:44px;box-sizing:border-box}}
#tb button{{background:#16213e;color:#fff;border:1px solid #0f3460;padding:4px 12px;
    border-radius:4px;cursor:pointer;font-size:12px}}
#tb button:hover{{background:#0f3460}}
#tb button.export-btn{{background:#1a5276;border-color:#2980b9}}
#tb button.export-btn:hover{{background:#2980b9}}
.lg{{display:flex;gap:12px;align-items:center;margin-left:16px;flex-wrap:wrap}}
.lg-i{{display:flex;align-items:center;gap:4px;font-size:11px}}
.lg-d{{width:10px;height:10px;border-radius:2px}}
#st{{margin-left:auto;opacity:.6;font-size:12px}}
</style></head><body>
<div id="tb">
  <button onclick="gantt.ext.zoom.zoomIn()">+ Zoom</button>
  <button onclick="gantt.ext.zoom.zoomOut()">− Zoom</button>
  <button onclick="eAll()">Expand</button>
  <button onclick="cAll()">Collapse</button>
  <button class="export-btn" onclick="exportPNG()">📸 Export PNG</button>
  <div class="lg">{legend_html}</div>
  <div id="st">Ready</div>
</div>
<div id="gantt_here"></div>
<script>
gantt.config.date_format         = "%Y-%m-%d";
gantt.config.drag_move           = true;
gantt.config.drag_resize         = true;
gantt.config.drag_links          = true;
gantt.config.drag_progress       = false;
gantt.config.show_links          = true;
gantt.config.row_height          = 30;
gantt.config.bar_height          = 20;
gantt.config.scale_height        = 50;
gantt.config.fit_tasks           = true;
gantt.config.open_tree_initially = true;
gantt.config.min_column_width    = 40;
gantt.config.columns = [
  {{name:"text",       label:"Task",  tree:true, width:260, resize:true}},
  {{name:"start_date", label:"Start", align:"center", width:85}},
  {{name:"duration",   label:"Days",  align:"center", width:45}},
  {{name:"stage",      label:"Stage", align:"center", width:100, resize:true}}
];
gantt.ext.zoom.init({{levels:[
  {{name:"Days",     scale_height:60, min_column_width:30,
    scales:[{{unit:"month",step:1,format:"%F %Y"}},{{unit:"day",step:1,format:"%d"}}]}},
  {{name:"Weeks",    scale_height:60, min_column_width:70,
    scales:[{{unit:"month",step:1,format:"%F %Y"}},{{unit:"week",step:1,format:"W%W"}}]}},
  {{name:"Months",   scale_height:60, min_column_width:90,
    scales:[{{unit:"year",step:1,format:"%Y"}},{{unit:"month",step:1,format:"%M"}}]}},
  {{name:"Quarters", scale_height:60, min_column_width:90,
    scales:[{{unit:"year",step:1,format:"%Y"}},{{unit:"quarter",step:1,format:"Q%q"}}]}}
]}});
gantt.ext.zoom.setLevel("Weeks");

gantt.templates.grid_row_class = function(s,e,t){{
  return t.type === "project" ? "project_row" : "";
}};
gantt.templates.task_class = function(s,e,t){{
  if(t.is_canceled) return "canceled";
  if(t.is_approved) return "approved";
  return "";
}};

gantt.init("gantt_here");
gantt.parse({data_json});

// Today marker
var todayDate = gantt.date.str_to_date("%Y-%m-%d")("{today_str}");
gantt.addMarker({{
  start_date: todayDate,
  css:   "today",
  text:  "Today",
  title: "Today: {today_str}"
}});

var stEl = document.getElementById('st');
function ss(m){{ stEl.textContent = m; setTimeout(()=>stEl.textContent='Ready', 3000); }}

function nav(p){{
  var qs = new URLSearchParams(p).toString();
  window.parent.location.search = '?' + qs;
}}

gantt.attachEvent("onAfterTaskDrag", function(id, mode){{
  var t = gantt.getTask(id);
  if(t.type === "project") return;
  var fmt = gantt.date.date_to_str("%Y-%m-%d");
  ss("Saving...");
  nav({{gantt_action:"update_task", tid:t.odoo_id||id,
        s:fmt(t.start_date), e:fmt(t.end_date)}});
}});

gantt.attachEvent("onAfterLinkAdd", function(id, link){{
  ss("Saving dependency...");
  nav({{gantt_action:"add_link", src:link.source, tgt:link.target}});
}});

gantt.attachEvent("onAfterLinkDelete", function(id, link){{
  ss("Removing dependency...");
  nav({{gantt_action:"delete_link", src:link.source, tgt:link.target}});
}});

function eAll(){{ gantt.eachTask(t=>{{t.$open=true}});  gantt.render(); }}
function cAll(){{ gantt.eachTask(t=>{{t.$open=false}}); gantt.render(); }}

function exportPNG(){{
  ss("Generating PNG…");
  gantt.eachTask(t=>{{t.$open=true}}); gantt.render();
  var el = document.getElementById("gantt_here");
  html2canvas(el, {{
    scale: 2,
    useCORS: true,
    backgroundColor: "#ffffff",
    width:  el.scrollWidth,
    height: el.scrollHeight,
    windowWidth:  el.scrollWidth,
    windowHeight: el.scrollHeight
  }}).then(function(canvas){{
    var link = document.createElement("a");
    var ts   = new Date().toISOString().slice(0,10);
    link.download = "INOVUES_Gantt_" + ts + ".png";
    link.href = canvas.toDataURL("image/png");
    link.click();
    ss("PNG downloaded ✓");
  }}).catch(function(err){{
    ss("Export failed: " + err);
  }});
}}
</script></body></html>"""


# ─── Sidebar: project filter + refresh ─────────────────────────
with st.sidebar:
    st.markdown("### 📁 Projects")

    try:
        projects = load_projects()
        tasks    = load_tasks()
    except Exception as e:
        st.error(f"❌ Could not connect to Odoo: {e}")
        st.stop()

    all_project_ids   = [p["id"] for p in projects]
    all_project_names = {p["id"]: p["name"] for p in projects}

    if "selected_projects" not in st.session_state:
        st.session_state.selected_projects = all_project_ids[:]

    col1, col2 = st.columns(2)
    with col1:
        if st.button("All", use_container_width=True):
            st.session_state.selected_projects = all_project_ids[:]
            st.rerun()
    with col2:
        if st.button("None", use_container_width=True):
            st.session_state.selected_projects = []
            st.rerun()

    for pid in all_project_ids:
        checked = pid in st.session_state.selected_projects
        new_val = st.checkbox(all_project_names[pid], value=checked, key=f"proj_{pid}")
        if new_val and pid not in st.session_state.selected_projects:
            st.session_state.selected_projects.append(pid)
        elif not new_val and pid in st.session_state.selected_projects:
            st.session_state.selected_projects.remove(pid)

    st.markdown("---")
    if st.button("🔄 Refresh from Odoo", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption("Auto-refreshes every 60s")


# ─── Render ────────────────────────────────────────────────────
st.markdown("""<style>
.block-container{padding-top:.5rem;padding-bottom:0}
header{visibility:hidden}
iframe{border:none!important}
</style>""", unsafe_allow_html=True)

if write_status:
    kind, msg = write_status
    st.toast(msg, icon="✅" if kind == "success" else "❌")

selected_ids = st.session_state.get("selected_projects", all_project_ids)

gantt_data, gantt_links, missing_dates, color_map = build_gantt_data(
    projects, tasks, set(selected_ids)
)

html = build_gantt_html(gantt_data, gantt_links, color_map, projects, set(selected_ids))
components.html(html, height=750, scrolling=False)

if missing_dates:
    with st.expander(f"⚠️ {len(missing_dates)} tasks missing dates — not on Gantt"):
        for t in missing_dates:
            proj = t["project_id"][1] if t["project_id"] else "No project"
            st.caption(f"**{t['name']}** — {proj}")
