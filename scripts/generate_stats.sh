#!/bin/bash
set -u

OUT=/home/main/weather-viewer/web/static/stats
LOG=/var/log/nginx
GA="goaccess --log-format=COMBINED --ignore-crawlers --keep-last=30"

run_site() {
    local name="$1"
    local label="$2"
    sudo bash -c "zcat -f ${LOG}/${name}.access.log ${LOG}/${name}.access.log.1 ${LOG}/${name}.access.log.*.gz 2>/dev/null | ${GA} --html-report-title='${label}' -o ${OUT}/${name}.html -" 2>/dev/null
}

mkdir -p "$OUT"

run_site vibeprojects "vibeprojects.us (weather, taostats, bitcoin, otc)"
run_site taoflow "taoflow.us"
run_site nebraskastormchasers "nebraskastormchasers.com"

cat > "${OUT}/index.html" <<HTML
<!doctype html>
<html><head><meta charset="utf-8"><title>Site Stats</title>
<style>
body{font-family:system-ui,sans-serif;max-width:640px;margin:3rem auto;padding:0 1rem;color:#222}
h1{font-size:1.4rem;margin-bottom:.25rem}
p.sub{color:#666;margin-top:0}
ul{list-style:none;padding:0}
li{margin:.75rem 0}
a{display:block;padding:.9rem 1rem;background:#f4f4f6;border-radius:8px;text-decoration:none;color:#0a4ea2;border:1px solid #e3e3e8}
a:hover{background:#eaeaef}
small{color:#666;display:block;margin-top:.2rem;font-weight:normal}
.foot{color:#888;font-size:.85rem;margin-top:2rem}
</style></head><body>
<h1>Site traffic</h1>
<p class="sub">Self-identified bots filtered. Last 30 days, hourly refresh.</p>
<ul>
  <li><a href="vibeprojects.html"><b>vibeprojects.us</b><small>weather, taostats, bitcoin, otc</small></a></li>
  <li><a href="taoflow.html"><b>taoflow.us</b><small>tao flows</small></a></li>
  <li><a href="nebraskastormchasers.html"><b>nebraskastormchasers.com</b><small>storm chasing</small></a></li>
</ul>
<p class="foot">Generated $(date '+%Y-%m-%d %H:%M %Z')</p>
</body></html>
HTML
