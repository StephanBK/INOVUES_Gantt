"""
INOVUES Project Gantt — Cross-project Gantt chart with two-way Odoo sync.
Visual rules:
  - Approved  → solid green bar
  - In Progress → diagonal stripe pattern (project color)
  - Other       → solid project color (fallback)
"""

import streamlit as st
import streamlit.components.v1 as components
import json
import xmlrpc.client
import os
import io
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
import matplotlib.patheffects as pe
import numpy as np

st.set_page_config(page_title="INOVUES Gantt", layout="wide", page_icon="📊")

# ─── Odoo connection ────────────────────────────────────────────
ODOO_URL     = os.environ.get("ODOO_URL",  "https://inovues.odoo.com")
ODOO_DB      = os.environ.get("ODOO_DB",   "inovues")
ODOO_USER    = os.environ.get("ODOO_USER", "sketterer@inovues.com")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")

COLOR_APPROVED = "#27ae60"   # solid green


def classify_stage(stage_name: str, state_val: str) -> str:
    """Return 'approved', 'in_progress', or 'other' — robust to casing/spacing."""
    s = (stage_name or "").strip().lower()
    v = (state_val  or "").strip().lower()
    if "approved" in s or "approved" in v:
        return "approved"
    if "progress" in s or "progress" in v or s == "in_progress" or v == "in_progress":
        return "in_progress"
    return "other"


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


# ─── Write-back actions ─────────────────────────────────────────
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
                     [[task_id], {"planned_date_begin": f"{start} 08:00:00",
                                  "date_deadline":      f"{end} 17:00:00"}])
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


# ─── Load data ──────────────────────────────────────────────────
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


PROJECT_COLORS = [
    "#2E86AB", "#A23B72", "#F18F01", "#C73E1D",
    "#3B1F2B", "#44BBA4", "#E94F37", "#393E41"
]


# ─── Build data for interactive Gantt ───────────────────────────
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
        gantt_data.append({"id": f"p_{pid}", "text": proj["name"],
                            "type": "project", "open": True,
                            "color": color_map[pid]})

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
            end = (datetime.strptime(start[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        elif end and not start:
            start = (datetime.strptime(end[:10], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

        start_str = start[:10]
        end_str   = end[:10]
        try:
            dur = max((datetime.strptime(end_str, "%Y-%m-%d") -
                       datetime.strptime(start_str, "%Y-%m-%d")).days, 1)
        except (ValueError, TypeError):
            dur = 1

        stage      = task["stage_id"][1] if task["stage_id"] else ""
        task_state = task.get("state", "")
        status     = classify_stage(stage, task_state)
        proj_color = color_map.get(proj_id, "#999")

        # For the interactive chart: approved=green, in_progress=project color
        # The stripe pattern is handled via CSS background on the bar element
        bar_color = COLOR_APPROVED if status == "approved" else proj_color

        gantt_data.append({
            "id":          task["id"],
            "text":        task["name"],
            "start_date":  start_str,
            "duration":    dur,
            "parent":      f"p_{proj_id}",
            "color":       bar_color,
            "proj_color":  proj_color,
            "stage":       stage,
            "state":       task_state,
            "status":      status,   # 'approved' | 'in_progress' | 'other'
            "odoo_id":     task["id"],
        })

        for dep_id in task.get("depend_on_ids", []):
            gantt_links.append({"id": f"L{dep_id}_{task['id']}",
                                 "source": dep_id, "target": task["id"], "type": "0"})

    return gantt_data, gantt_links, missing_dates, color_map


# ─── Server-side PNG export ──────────────────────────────────────
def render_gantt_png(projects, tasks):
    full_color_map = {}
    for i, proj in enumerate(projects):
        full_color_map[proj["id"]] = PROJECT_COLORS[i % len(PROJECT_COLORS)]

    proj_task_map = {}
    for task in tasks:
        if not task["project_id"]:
            continue
        proj_task_map.setdefault(task["project_id"][0], []).append(task)

    rows = []
    for proj in projects:
        pid    = proj["id"]
        ptasks = proj_task_map.get(pid, [])
        dated  = []
        for t in ptasks:
            start = t.get("planned_date_begin")
            end   = t.get("date_deadline")
            if not start and not end:
                continue
            if start and not end:
                end = (datetime.strptime(start[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            elif end and not start:
                start = (datetime.strptime(end[:10], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            dated.append((t, start[:10], end[:10]))

        if not dated:
            continue

        all_starts = [datetime.strptime(s, "%Y-%m-%d") for _, s, _ in dated]
        all_ends   = [datetime.strptime(e, "%Y-%m-%d") for _, _, e in dated]
        rows.append({"label": proj["name"], "start": min(all_starts),
                     "end": max(all_ends), "color": full_color_map.get(pid, "#666"),
                     "is_header": True, "status": "header",
                     "proj_color": full_color_map.get(pid, "#666")})

        for t, s, e in dated:
            stage      = t["stage_id"][1] if t["stage_id"] else ""
            task_state = t.get("state", "")
            status     = classify_stage(stage, task_state)
            proj_color = full_color_map.get(pid, "#999")
            bar_color  = COLOR_APPROVED if status == "approved" else proj_color
            rows.append({"label": f"  {t['name']}", "start": datetime.strptime(s, "%Y-%m-%d"),
                         "end": datetime.strptime(e, "%Y-%m-%d"), "color": bar_color,
                         "is_header": False, "status": status, "proj_color": proj_color})

    if not rows:
        return None

    import matplotlib.dates as mdates

    n_rows     = len(rows)
    row_height = 0.38
    fig_height = max(8, n_rows * row_height + 3)
    fig_width  = 28

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    fig.patch.set_facecolor("#f8f9fa")
    ax.set_facecolor("#ffffff")

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    all_starts = [r["start"] for r in rows]
    all_ends   = [r["end"]   for r in rows]
    x_min = min(all_starts) - timedelta(days=14)
    x_max = max(all_ends)   + timedelta(days=14)

    ax.set_xlim(x_min, x_max)
    ax.set_ylim(-0.5, n_rows - 0.5)
    ax.invert_yaxis()

    # Alternating row backgrounds
    for i in range(n_rows):
        ax.axhspan(i - 0.5, i + 0.5,
                   color="#f0f4f8" if i % 2 == 0 else "#ffffff", zorder=0)

    # Weekend shading
    d = x_min
    while d <= x_max:
        if d.weekday() >= 5:
            ax.axvspan(d, d + timedelta(days=1), color="#ececec", alpha=0.5, zorder=0)
        d += timedelta(days=1)

    # Draw bars
    for i, row in enumerate(rows):
        s   = mdates.date2num(row["start"])
        e   = mdates.date2num(row["end"])
        dur = max(e - s, 0.5)
        h   = 0.55 if row["is_header"] else 0.45
        y   = i - h / 2

        if row["status"] == "in_progress":
            # Draw base bar (slightly transparent)
            base = FancyBboxPatch((s, y), dur, h,
                                  boxstyle="round,pad=0.01",
                                  facecolor=row["proj_color"],
                                  edgecolor="white", linewidth=0.6,
                                  alpha=0.35, zorder=2)
            ax.add_patch(base)

            # Diagonal stripe overlay using hatch
            hatch_bar = FancyBboxPatch((s, y), dur, h,
                                       boxstyle="round,pad=0.01",
                                       facecolor="none",
                                       edgecolor=row["proj_color"],
                                       linewidth=0.8,
                                       hatch="////",
                                       zorder=3)
            ax.add_patch(hatch_bar)
        else:
            # Solid bar (approved = green, header = project color, other = project color)
            rect = FancyBboxPatch((s, y), dur, h,
                                  boxstyle="round,pad=0.01",
                                  facecolor=row["color"],
                                  edgecolor="white", linewidth=0.6,
                                  zorder=2)
            ax.add_patch(rect)

        fontsize   = 7.5 if row["is_header"] else 6.5
        fontweight = "bold" if row["is_header"] else "normal"
        txt_color  = "white" if row["status"] != "in_progress" else row["proj_color"]
        mid = s + dur / 2
        ax.text(mid, i, row["label"].strip(),
                ha="center", va="center",
                fontsize=fontsize, fontweight=fontweight,
                color=txt_color, clip_on=True, zorder=4)

    # Y-axis labels
    ax.set_yticks(range(n_rows))
    ax.set_yticklabels([r["label"] for r in rows], fontsize=7, fontfamily="monospace")
    for tick, row in zip(ax.get_yticklabels(), rows):
        tick.set_fontweight("bold" if row["is_header"] else "normal")
        tick.set_color("#1a1a2e" if row["is_header"] else "#333333")

    # X axis
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_minor_locator(mdates.WeekdayLocator(byweekday=0))
    ax.grid(axis="x", which="major", color="#cccccc", linewidth=0.8, zorder=1)
    ax.grid(axis="x", which="minor", color="#eeeeee", linewidth=0.4, zorder=1)
    plt.xticks(rotation=30, ha="right", fontsize=8)

    # Today line
    ax.axvline(mdates.date2num(today), color="#DC143C", linewidth=2,
               linestyle="--", zorder=5)
    ax.text(mdates.date2num(today), -0.4, "TODAY",
            color="#DC143C", fontsize=7, fontweight="bold",
            ha="center", va="top", zorder=6)

    # Legend
    legend_patches = []
    for proj in projects:
        pid = proj["id"]
        if pid in full_color_map:
            legend_patches.append(mpatches.Patch(
                color=full_color_map[pid], label=proj["name"]))
    legend_patches.append(mpatches.Patch(color=COLOR_APPROVED, label="✓ Approved (solid)"))
    # In Progress swatch: show hatch
    ip_patch = mpatches.Patch(facecolor="#aaaaaa", edgecolor="#555555",
                               hatch="////", label="⟳ In Progress (striped)")
    legend_patches.append(ip_patch)

    ax.legend(handles=legend_patches, loc="upper left", fontsize=7,
              framealpha=0.9, ncol=min(len(legend_patches), 6),
              bbox_to_anchor=(0, 1.02), borderaxespad=0)

    ts = today.strftime("%B %d, %Y")
    fig.suptitle(f"INOVUES — Full Project Gantt   ·   {ts}",
                 fontsize=13, fontweight="bold", color="#1a1a2e", y=0.99)

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout(rect=[0, 0, 1, 0.97])

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf


# ─── HTML Gantt ─────────────────────────────────────────────────
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
        f'<div class="lg-i">'
        f'<div class="lg-d" style="background:repeating-linear-gradient('
        f'45deg,#888 0px,#888 3px,#ccc 3px,#ccc 7px)"></div>'
        f'<span>In Progress</span></div>'
    )

    today_str = datetime.now().strftime("%Y-%m-%d")
    data_json = json.dumps({"data": gantt_data, "links": gantt_links})

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script src="https://cdn.dhtmlx.com/gantt/edge/dhtmlxgantt.js"></script>
<link  rel="stylesheet" href="https://cdn.dhtmlx.com/gantt/edge/dhtmlxgantt.css">
<style>
html,body{{margin:0;padding:0;height:100%;overflow:hidden;font-family:'Segoe UI',system-ui,sans-serif}}
#gantt_here{{width:100%;height:calc(100vh - 44px)}}
.project_row{{font-weight:700;background:#f0f0f0!important}}
.project_row .gantt_cell{{font-weight:700}}
.gantt_task_content{{font-size:11px;font-weight:500;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.4)}}
/* Approved: solid, bold label */
.approved .gantt_task_content{{font-weight:700}}
/* In Progress: diagonal stripe — must beat DHTMLX specificity */
.gantt_task_line.in_progress,
.in_progress > .gantt_task_line,
.gantt_task_row .in_progress .gantt_task_line {{
  background-image: repeating-linear-gradient(
    45deg,
    transparent 0px,
    transparent 5px,
    rgba(255,255,255,0.4) 5px,
    rgba(255,255,255,0.4) 10px
  ) !important;
  background-color: inherit !important;
}}
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
.lg{{display:flex;gap:12px;align-items:center;margin-left:16px;flex-wrap:wrap}}
.lg-i{{display:flex;align-items:center;gap:4px;font-size:11px}}
.lg-d{{width:14px;height:10px;border-radius:2px;border:1px solid rgba(255,255,255,0.2)}}
#st{{margin-left:auto;opacity:.6;font-size:12px}}
</style></head><body>
<div id="tb">
  <button onclick="gantt.ext.zoom.zoomIn()">+ Zoom</button>
  <button onclick="gantt.ext.zoom.zoomOut()">− Zoom</button>
  <button onclick="eAll()">Expand</button>
  <button onclick="cAll()">Collapse</button>
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
  {{name:"stage",      label:"Stage", align:"center", width:110, resize:true}}
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
  if(t.status === "approved")    return "approved";
  if(t.status === "in_progress") return "in_progress";
  return "";
}};

gantt.init("gantt_here");
gantt.parse({data_json});

var todayDate = gantt.date.str_to_date("%Y-%m-%d")("{today_str}");
gantt.addMarker({{start_date:todayDate, css:"today", text:"Today", title:"Today: {today_str}"}});

var stEl = document.getElementById('st');
function ss(m){{ stEl.textContent=m; setTimeout(()=>stEl.textContent='Ready',3000); }}
function nav(p){{ window.parent.location.search='?'+new URLSearchParams(p).toString(); }}

gantt.attachEvent("onAfterTaskDrag",function(id,mode){{
  var t=gantt.getTask(id);
  if(t.type==="project")return;
  var fmt=gantt.date.date_to_str("%Y-%m-%d");
  ss("Saving...");
  nav({{gantt_action:"update_task",tid:t.odoo_id||id,s:fmt(t.start_date),e:fmt(t.end_date)}});
}});
gantt.attachEvent("onAfterLinkAdd",function(id,link){{
  ss("Saving dependency...");
  nav({{gantt_action:"add_link",src:link.source,tgt:link.target}});
}});
gantt.attachEvent("onAfterLinkDelete",function(id,link){{
  ss("Removing dependency...");
  nav({{gantt_action:"delete_link",src:link.source,tgt:link.target}});
}});

gantt.attachEvent("onGanttRender", function(){{
  setTimeout(applyStripes, 0);
}});

function applyStripes(){{
  gantt.eachTask(function(t){{
    if(t.type === "project") return;
    var el = document.querySelector(".gantt_task_line[task_id='" + t.id + "']");
    if(!el) return;
    if(t.status === "in_progress"){{
      el.style.backgroundImage = "repeating-linear-gradient(45deg, transparent 0px, transparent 5px, rgba(255,255,255,0.45) 5px, rgba(255,255,255,0.45) 10px)";
      el.style.backgroundSize  = "14px 14px";
    }} else {{
      el.style.backgroundImage = "";
    }}
  }});
}}

function eAll(){{ gantt.eachTask(t=>{{t.$open=true}});  gantt.render(); setTimeout(applyStripes,50); }}
function cAll(){{ gantt.eachTask(t=>{{t.$open=false}}); gantt.render(); setTimeout(applyStripes,50); }}
</script></body></html>"""


# ─── Sidebar ────────────────────────────────────────────────────
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
        if st.button("All",  use_container_width=True):
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

    st.markdown("---")
    st.markdown("### 📸 Export")
    st.caption("All tasks · all projects · ignores filter")
    if st.button("Generate PNG", use_container_width=True, type="primary"):
        with st.spinner("Rendering full Gantt…"):
            png_buf = render_gantt_png(projects, tasks)
        if png_buf:
            fname = f"INOVUES_Gantt_{datetime.now().strftime('%Y-%m-%d')}.png"
            st.download_button("⬇️ Download PNG", data=png_buf,
                               file_name=fname, mime="image/png",
                               use_container_width=True)
        else:
            st.warning("No tasks with dates found.")

    # ── Debug: show raw stage values so you can verify matching ──
    st.markdown("---")
    with st.expander("🔍 Debug — raw stage values"):
        stage_counts = {}
        for t in tasks:
            sname = t["stage_id"][1] if t["stage_id"] else "(none)"
            sval  = t.get("state", "(none)")
            cls   = classify_stage(sname, sval)
            key   = f"stage='{sname}'  state='{sval}'  → {cls}"
            stage_counts[key] = stage_counts.get(key, 0) + 1
        for k, v in sorted(stage_counts.items(), key=lambda x: -x[1]):
            st.caption(f"{v}× {k}")


# ─── Main render ────────────────────────────────────────────────
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
    projects, tasks, set(selected_ids))
html = build_gantt_html(gantt_data, gantt_links, color_map, projects, set(selected_ids))
components.html(html, height=750, scrolling=False)

if missing_dates:
    with st.expander(f"⚠️ {len(missing_dates)} tasks missing dates — not on Gantt"):
        for t in missing_dates:
            proj = t["project_id"][1] if t["project_id"] else "No project"
            st.caption(f"**{t['name']}** — {proj}")
