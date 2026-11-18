# -*- coding: utf-8 -*-




"""
ai_clean.py — AI 干净节点筛选器 v3 (Windows / 复用 Clash Verge Rev 的 mihomo)

核心模型
--------
1) 解锁探测：一次性事实。服务级增量，只补 state 里还是 null 的项；
   拿到 1 或「强信号 0」即永久封盘，绝不重测。
   T1 = ChatGPT(web宽松+favicon复核) + YouTube Premium
   T2 = T1 + Claude + TikTok
   T3 = T2 + Netflix(完整) + Disney+ + Prime Video
   Apple GCC = 地区探针(不入门槛)，用于识别送中节点
2) 健康采样：唯一周期性工作。gstatic 204 测延迟/可达；
   失败时补 Apple GCC 区分「节点死了」与「活着但送中」。
3) 架构：1 个 mihomo 实例 + 静态 listeners 端口池(每节点钉死一个本地端口)
   + Python 线程池(并发硬上限 3)。测试期间零切换、零重载。

依赖: pip install requests pyyaml rich

用法
----
  # 从现有 clash 配置生成订阅清单(一次性)
  python ai_clean.py --from-clash myclashyaml.yaml --dump-subs subs.txt

  # 日常/定时任务就跑这一条：有新节点测解锁，没有就只做健康采样
  python ai_clean.py --subs subs.txt --out-tiers t1,t2,t3

  # 定时任务向导(安装/卸载/状态)
  python ai_clean.py --install-task
  python ai_clean.py --uninstall-task
  python ai_clean.py --task-status

  # 离线：只看汇总 / 只重新生成 yaml
  python ai_clean.py --status
  python ai_clean.py --emit-only --out-tiers t1,t2,t3

"""



# ============================================================================
# 【AI-HANDOFF / 架构速览】给后续 AI agent 与未来自己的交接文档 (2026-11-15)
# 本文件 2000+ 行。改代码前先读完本块，遵守"不变量"与"禁区"。
#
# ■ 用途与依赖
#   从多机场订阅筛"AI/流媒体干净节点": 一次性增量解锁探测(定档 T1/T2/T3)
#   + 周期性健康采样(稳定性/延迟)，输出三档 clash yaml 供主配置 file-provider 引用。
#   Windows 优先(复用 Clash Verge Rev 的 verge-mihomo)，Linux 亦支持。
#   依赖: requests pyyaml rich；可选 curl_cffi(订阅 TLS 伪装，强烈建议装)。
#
# ■ 核心模型(不变量，勿破坏)
#   1. 解锁=一次性事实: services[s].v ∈ {None未定论, 1解锁, 0强拒绝/止损}。
#      定论即封盘(sealed)永不重测；只有 --force-unlock 可重置。
#   2. T1=chatgpt+youtube; T2=T1+claude+tiktok; T3=T2+netflix(完整)+disney+primevideo。
#      T1 任一项 0 → block(永久封盘)；T2/T3 项 0 → 封顶当前档。
#      Apple GCC 只是地区探针(不入门槛)，命中 DEFAULT_BLOCKED_REGIONS(送中) → block。
#   3. 健康采样是唯一周期工作: 控制API /proxies/{name}/delay 测 gstatic 204；
#      失败且有 listener 时补 Apple 区分"死了/送中"。recent 窗口 96 点(30min心跳=48h)。
#   4. 每批 1 个 mihomo 实例 + listeners 端口池，listener.proxy 钉死节点；
#      rules=MATCH,DIRECT 与 dns.enable=False 都是故意的(listener 优先级高于路由)。
#      测试期间零切换零重载。并发硬上限 MAX_CONCURRENCY=3，勿调高(机场风控)。
#   5. RunLock(state 旁 .lock，6h 过期): 并发实例直接退出，勿绕过。
#   6. ai_state.json 是唯一事实源，每批落盘(atomic_write)。加字段必须在
#      load_state() 的 setdefault 兼容循环里补，勿改已有字段语义。
#
# ■ 代码地图(按 "==== 区名 ====" 搜索定位)
#   常量: UA/探针URL/匿名cookie/TIER_*/RECENT_WINDOW/MAX_CONCURRENCY
#   小工具: log/atomic_write/mask_url/parse_tiers...
#   分享链接解析: parse_body → clash yaml 或 ss/vmess/trojan/vless/hy2/tuic/anytls 兜底
#   订阅: Sub / fetch_sub(已反风控改造，见下) / load_subs_file / extract_from_clash
#   节点清洗: sanitize_proxy(剥引用字段+修 reality short-id) / fingerprint(sha1前16位)
#   状态: new_node / load_state / save_state / tier_pass / finalize / block /
#         unlock_pending / stability*
#   mihomo: find_mihomo / alloc_ports / MihomoInstance / build_config /
#           make_session(浏览器基础头) / http(统一请求包装，返回 (resp, err))
#   探针: probe_delay(控制API) / apple / chatgpt(compliance+favicon复核) / youtube /
#         claude / tiktok / netflix(双片名) / disney(bamgrid三步) / primevideo
#   解锁状态机: do_unlock(增量+tier门槛+止损) / health_sample / worker(线程入口)
#   批次运行: needs_listener(决定是否分配端口) / run_batch_once / run_batch(内核拒绝二分)
#   输出: apply_filters(★故意空实现，用户要求，勿擅自加筛选) / emit(三档yaml)
#   汇总展示: show_status / stability_rows / dump_stats / show_stability
#   定时任务向导: Windows 计划任务 / Linux systemd user timer + cron 回退
#   锁: RunLock；主流程: main / build_parser / refresh_subscriptions / run_round
#
# ■ 已完成改造(2026-11-15)
#   - 订阅反风控: fetch_sub 优先 curl_cffi(impersonate=chrome，伪装 TLS/JA3+HTTP2)，
#     未装退回 requests；UA 必须保持 clash 系(机场按 UA 决定返回格式，勿换浏览器 UA)；
#     Session 自动 cookie + --sub-cookie 手动 WAF cookie；403/429/503 退避(含 Retry-After)；
#     订阅间 --sub-interval+抖动；do_unlock 服务间 0.4~1.2s 抖动；make_session 补浏览器头。
#   - --help: build_parser() 分组+示例，是参数唯一定义入口；main 里只做校验。
#
# ■ IP纯净度独立单元(已实现，低耦合，支持 IPQS + ipapi.is + AbuseIPDB)
#   目标: 节点出口IP → IPQS(必须) + ipapi.is(可选, ASN/机房滥用率) + AbuseIPDB(可选,
#         真实攻击举报) 综合评估纯净度，辅助筛选 T2/T3 高质量养号机房 IP。
#   只记录+展示+导出；默认不封盘；绝不动 services/tier/sealed；不拉订阅。
#   形态: 自包含区段 "==== IP 纯净度独立单元 ===="，核心函数:
#     detect_local_ip() / detect_exit_ip(sess,args) / query_ipqs / query_ipapi /
#     query_abuseipdb / do_ip_purity(n,sess,args) / ipqs_pending(n,args) /
#     compute_purity(n) / show_ipqs(st,args)
#   对既有代码的改动收敛为以下钩子，每处用 "# [IPQS]" 注释标记，便于审查/回退:
#     H1 new_node/load_state: 补 "ip":{}, "ipqs":{}, "ipapi":{}, "abuseipdb":{} 与顶层 "ip_cache":{}
#     H2 needs_listener: 各分支 OR needs_exit_ip_probe(n,args)
#     H3 worker: 末尾若 args.ipqs 且 ipqs_pending → do_ip_purity；新增 ipqs_only 分支
#     H4 run_round: targets 按 ipqs_only/min_tier 过滤；ipqs_only 不 finalize；
#        仅启用 --ipqs-block-above 时才 emit
#     H5 main: key 校验(--ipqs-key/IPQS_KEY, --ipapi-key/IPAPI_KEY, --abuseipdb-key/ABUSEIPDB_KEY)
#     H6 stability_rows 追加 region_yt/exit_ip/ipqs_*/ipapi_*/abuse_*/purity_* 列；--status 与跑完时调 show_ipqs
#     H7 build_parser: --ipqs* + --ipapi-key + --abuseipdb-key + --ipqs-min-tier
#   查询走直连 make_session(None)，全局 Lock 串行 + 间隔控制 + 按出口 IP 全局去重缓存
#   (st["ip_cache"])，不耗节点流量。安全: key 绝不写日志/state。
#
# ■ 陷阱与禁区(历史踩坑，勿"顺手修复")
#   - refresh_subscriptions 先把所有节点 present=False 再按拉取结果回填:
#     某机场拉取失败 → 该机场本轮全体离场(yaml 缩水)。这是设计行为，结论仍在
#     state，下次成功刷新自动回归。刷新前建议备份 ai_state.json。
#   - targets 恒排除 blocked(即使 --stability-only；旧 help 说"含已封盘"是错的)。
#   - worker 的 finally 必须 sess.close() + n["port"]=None；run_batch_once 也会清端口。
#   - --status / --emit-only 必须保持离线(不找 mihomo、不联网)。
#   - stability_only 不 finalize、不 emit；健康采样里送中封盘仅在非 stability_only 生效。
#   - 内核整批拒绝 → run_batch 二分排障(budget=25)，单节点 kernel_fail>=3 才封盘。
#   - apply_filters 空实现是用户明确要求；--min-stability/--region/--max-delay 预留未启用。
#   - --exclude-provider 仅作用于输出/报告(emit/status/stats), 不排除测试/重置/finalize;
#     订阅仍会拉取并入池, 定时任务安装时会原样带上该参数。
#   - Windows 计划任务引号用 _psq；勿改 CREATE_NO_WINDOW；保持 Linux 分支可用。
# ============================================================================

from __future__ import annotations

import getpass
import shlex
import argparse
import atexit
import base64
import hashlib
import json
import os
import random
import re
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Optional
from urllib.parse import urlsplit, parse_qs, unquote, quote
import ipaddress

try:
    import requests
    import yaml
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, IntPrompt, Confirm
    from rich.progress import (Progress, BarColumn, TextColumn,
                               TimeElapsedColumn, TimeRemainingColumn)
    from rich import box
except ImportError:
    print("缺少依赖，请先执行:  pip install requests pyyaml rich")
    sys.exit(1)

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

# ============================== 常量 ==============================
UA_SUB = "clash-verge/v2.0.2"
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
SEC_CH_UA = '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"'

YT_COOKIE = ("YSC=FSCWhKo2Zgw; VISITOR_PRIVACY_METADATA=CgJERRIEEgAgYQ%3D%3D; "
             "PREF=f7=4000; __Secure-YEC=CgtRWTBGTFExeV9Iayjele2yBjIKCgJERRIEEgAgYQ%3D%3D; "
             "SOCS=CAISOAgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwNTI2LjAxX3AwGgV6aC1DTiACGgYIgMnpsgY; "
             "VISITOR_INFO1_LIVE=Di84mAIbgKY; __Secure-BUCKET=CGQ")
# Netflix 匿名 cookie(仅 consent 相关, 不含任何账号凭据)
NF_COOKIE = ("flwssn=d2c72c47-49e9-48da-b7a2-2dc6d7ca9fcf; "
             "nfvdid=BQFmAAEBEMZa4XMYVzVGf9-kQ1HXumtAKsCyuBZU4QStC6CGEGIVznjNuuTerLAG8v2-9V_kYhg5uxTB5_yyrmqc02U5l1Ts74Qquezc9AE-LZKTo3kY3g%3D%3D; "
             "OptanonConsent=isGpcEnabled=0&version=202411.1.0&browserGpcFlag=0&isIABGlobal=false&hosts=&"
             "consentId=f13f841e-c75d-4f95-ab04-d8f581cac53e&interactionCount=0&isAnonUser=1&"
             "groups=C0001%3A1%2CC0002%3A1%2CC0003%3A1%2CC0004%3A1")
DISNEY_AUTH = "ZGlzbmV5JmJyb3dzZXImMS4wLjA.Cu56AgSfBTDag5NiRA81oLHkDZfu5L3CKadnefEAY84"

URL_APPLE = "https://gspe1-ssl.ls.apple.com/pep/gcc"
URL_OPENAI = "https://api.openai.com/compliance/cookie_requirements"
URL_OPENAI_FAVICON = "https://chatgpt.com/favicon.ico"
URL_YT = "https://www.youtube.com/premium"
URL_CLAUDE = "https://claude.ai/"
URL_TIKTOK = "https://www.tiktok.com/"
URL_NF_1 = "https://www.netflix.com/title/81280792"   # LEGO Ninjago (非自制)
URL_NF_2 = "https://www.netflix.com/title/70143836"   # Breaking Bad
URL_PV = "https://www.primevideo.com"
URL_DELAY = "https://www.gstatic.com/generate_204"

SERVICES = ["chatgpt", "youtube", "claude", "tiktok", "netflix", "disney", "primevideo"]
T1 = ["chatgpt", "youtube"]
T2 = T1 + ["claude", "tiktok"]
T3 = T2 + ["netflix", "disney", "primevideo"]
TIER_OF = {"chatgpt": 1, "youtube": 1, "claude": 2, "tiktok": 2,
           "netflix": 3, "disney": 3, "primevideo": 3}
TIER_FILES = {1: "ai-sub-t1-chatgpt-yt.yaml",
              2: "ai-sub-t2-claude-tiktok.yaml",
              3: "ai-sub-t3-netflix-disney-pv.yaml"}
TIER_DESC = {1: "ChatGPT + YouTube Premium",
             2: "T1 + Claude + TikTok",
             3: "T2 + Netflix(完整) + Disney+ + Prime Video"}

DEFAULT_EXCLUDE_KEYWORDS = ["套餐到期", "剩余流量", "剩余", "导航页",
                            "更新订阅", "官网", "禁止注册机", "过滤掉"]
DEFAULT_BLOCKED_REGIONS = ["CN", "RU", "IR", "KP", "CU", "SY"]
STRIP_FIELDS = {"dialer-proxy", "interface-name", "routing-mark"}
RECENT_WINDOW = 96          # 30min × 96 = 48h
MAX_CONCURRENCY = 3
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

console = Console()
_LOG_FH = None


# ============================== 小工具 ==============================
def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str):
    if _LOG_FH:
        try:
            _LOG_FH.write(f"[{now_str()}] {msg}\n")
            _LOG_FH.flush()
        except Exception:
            pass


def b64d(s: str) -> str:
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s).decode("utf-8", "ignore")


def human_size(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"


def mask_url(url: str) -> str:
    u = re.sub(r"(token=)[^&]+", r"\1***", url, flags=re.I)
    u = re.sub(r"(OwO=)[^&]+", r"\1***", u, flags=re.I)
    return re.sub(r"[0-9a-f]{32}", "***", u)


def rand_secret(n=12) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


def split_kw(s: str) -> list[str]:
    return [k.strip() for k in re.split(r"[,，]", s) if k.strip()]


def is_info_node(name: str, keywords: list[str]) -> Optional[str]:
    low = name.lower()
    for kw in keywords:
        if kw and kw.lower() in low:
            return kw
    return None


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def log_tail(path: Path, n=15) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:])
    except Exception:
        return "(无日志)"


def parse_tiers(s: str) -> list[int]:
    out = []
    for tok in re.split(r"[,，\s]+", s.strip().lower()):
        if not tok:
            continue
        t = tok.lstrip("t")
        if t not in ("1", "2", "3"):
            raise ValueError(f"档位无效: {tok} (只支持 t1/t2/t3)")
        if int(t) not in out:
            out.append(int(t))
    return sorted(out) or [1]


# ============================== 分享链接解析(兜底) ==============================
def _parse_ss(line: str) -> Optional[dict]:
    body, name = line[5:], ""
    if "#" in body:
        body, frag = body.split("#", 1)
        name = unquote(frag)
    if "@" in body:
        ui, hp = body.rsplit("@", 1)
        dec = b64d(ui)
        if ":" not in dec:
            dec = unquote(ui)
        if ":" not in dec:
            return None
        method, password = dec.split(":", 1)
        parts = hp.split("?", 1)
        host, _, port = parts[0].rstrip("/").rpartition(":")
        p = {"name": name or f"ss-{host}", "type": "ss", "server": host, "port": int(port),
             "cipher": method, "password": password, "udp": True}
        q = {k: v[0] for k, v in parse_qs(parts[1] if len(parts) > 1 else "").items()}
        plugin = q.get("plugin", "")
        opts = dict(kv.split("=", 1) for kv in plugin.split(";")[1:] if "=" in kv)
        if plugin.startswith("obfs"):
            p["plugin"] = "obfs"
            p["plugin-opts"] = {"mode": opts.get("obfs", "http"), "host": opts.get("obfs-host", "")}
        elif plugin.startswith("v2ray"):
            p["plugin"] = "v2ray-plugin"
            p["plugin-opts"] = {"mode": opts.get("mode", "websocket"), "host": opts.get("host", ""),
                                "path": opts.get("path", "/"), "tls": "tls" in plugin}
        return p
    m = re.match(r"([^:]+):(.+)@(.+):(\d+)$", b64d(body))
    if not m:
        return None
    return {"name": name or f"ss-{m.group(3)}", "type": "ss", "server": m.group(3),
            "port": int(m.group(4)), "cipher": m.group(1), "password": m.group(2), "udp": True}


def _parse_vmess(line: str) -> Optional[dict]:
    d = json.loads(b64d(line[8:].split("#", 1)[0].split("?", 1)[0]))
    p = {"name": d.get("ps") or f"vmess-{d['add']}", "type": "vmess", "server": d["add"],
         "port": int(d["port"]), "uuid": d["id"], "alterId": int(d.get("aid", 0)),
         "cipher": d.get("scy") or "auto", "udp": True}
    net, host = d.get("net", "tcp"), d.get("host") or ""
    if net in ("ws", "httpupgrade"):
        p["network"] = net
        ws = {"path": d.get("path", "/")}
        if host:
            ws["headers"] = {"Host": host}
        p["ws-opts"] = ws
    elif net == "grpc":
        p["network"] = "grpc"
        p["grpc-opts"] = {"grpc-service-name": d.get("path", "")}
    elif net == "h2":
        p["network"] = "h2"
        p["h2-opts"] = {"path": d.get("path", "/"), "host": [host] if host else []}
    if str(d.get("tls", "")).lower() in ("tls", "1", "true"):
        p["tls"] = True
    if d.get("sni"):
        p["servername"] = d["sni"]
    elif host and p.get("tls"):
        p["servername"] = host
    if d.get("fp"):
        p["client-fingerprint"] = d["fp"]
    if d.get("alpn"):
        p["alpn"] = d["alpn"].split(",") if isinstance(d["alpn"], str) else list(d["alpn"])
    return p


def _parse_uri(line: str, scheme: str) -> Optional[dict]:
    u = urlsplit(line)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    host, port = u.hostname or "", u.port or 443
    name = unquote(u.fragment) or f"{scheme}-{host}"
    pwd = unquote(u.username or "")
    p: dict = {"name": name, "server": host, "port": port, "udp": True}
    if scheme == "trojan":
        p.update(type="trojan", password=pwd)
    elif scheme == "vless":
        p.update(type="vless", uuid=pwd)
        if q.get("flow"):
            p["flow"] = q["flow"]
    elif scheme in ("hysteria2", "hy2"):
        p.update(type="hysteria2", password=pwd)
        if q.get("obfs"):
            p["obfs"] = q["obfs"]
            p["obfs-password"] = q.get("obfs-password", "")
    elif scheme == "tuic":
        p.update(type="tuic", uuid=pwd, password=unquote(u.password or ""))
        if q.get("congestion_control"):
            p["congestion-controller"] = q["congestion_control"]
        if q.get("udp_relay_mode"):
            p["udp-relay-mode"] = q["udp_relay_mode"]
    elif scheme == "anytls":
        p.update(type="anytls", password=pwd)
    else:
        return None
    if q.get("sni") or q.get("peer"):
        p["servername"] = q.get("sni") or q.get("peer")
    if q.get("allowInsecure", q.get("insecure", "0")) in ("1", "true"):
        p["skip-cert-verify"] = True
    if q.get("alpn"):
        p["alpn"] = q["alpn"].split(",")
    if q.get("fp"):
        p["client-fingerprint"] = q["fp"]
    sec = q.get("security", "")
    if scheme in ("trojan", "vless"):
        if sec in ("tls", "reality") or scheme == "trojan":
            p["tls"] = True
        if sec == "reality":
            p["reality-opts"] = {"public-key": q.get("pbk", ""), "short-id": q.get("sid", "")}
        net = q.get("type", "tcp")
        if net in ("ws", "httpupgrade"):
            p["network"] = net
            ws = {"path": q.get("path", "/")}
            if q.get("host"):
                ws["headers"] = {"Host": q["host"]}
            p["ws-opts"] = ws
        elif net == "grpc":
            p["network"] = "grpc"
            p["grpc-opts"] = {"grpc-service-name": q.get("serviceName", "")}
    return p


def parse_link(line: str) -> Optional[dict]:
    try:
        if line.startswith("ss://"):
            return _parse_ss(line)
        if line.startswith("vmess://"):
            return _parse_vmess(line)
        for sch in ("trojan://", "vless://", "hysteria2://", "hy2://", "tuic://", "anytls://"):
            if line.startswith(sch):
                return _parse_uri(line, sch[:-3])
    except Exception:
        return None
    return None


#  def parse_body(text: str) -> list[dict]:
    #  text = text.strip()
    #  cands = [text]
    #  try:
        #  cands.append(b64d(text))
    #  except Exception:
        #  pass
    #  for body in cands:
        #  try:
            #  data = yaml.safe_load(body)
            #  if isinstance(data, dict) and isinstance(data.get("proxies"), list):
                #  return [p for p in data["proxies"] if isinstance(p, dict)]
        #  except yaml.YAMLError:
            #  pass
    #  for body in cands:
        #  lines = [l.strip() for l in body.splitlines() if l.strip()]
        #  if lines and all(re.match(r"^[a-z0-9]+://", l) for l in lines):
            #  out = [p for p in (parse_link(l) for l in lines) if p]
            #  if out:
                #  return out
    #  return []
#  
def parse_body(text: str) -> list[dict]:
    """只接受机场返回的标准 clash/mihomo 配置(proxies: 或 payload:)。
    ★ 已禁用手写分享链接解析器：协议字段随上游演进，手写映射必错。"""
    text = text.strip()
    cands = [text]
    try:
        cands.append(b64d(text))      # 仅传输层解码，不是协议解析
    except Exception:
        pass
    for body in cands:
        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict):
            for key in ("proxies", "payload"):
                if isinstance(data.get(key), list):
                    out = [p for p in data[key] if isinstance(p, dict)]
                    if out:
                        log(f"PARSE yaml({key}): {len(out)} 节点")
                        return out
    log("PARSE 失败: 非 clash yaml(proxies/payload)。手写分享链接兜底已禁用")
    return []
# ============================== 订阅 ==============================
class Sub:
    def __init__(self, name: str, url: str):
        self.name, self.url = name, url


#  def fetch_sub(sub: Sub, ua: str) -> tuple[list[dict], str, str]:
    #  verify, err = True, ""
    #  for _ in range(3):
        #  try:
            #  r = requests.get(sub.url, headers={"User-Agent": ua}, timeout=(10, 30), verify=verify)
            #  r.raise_for_status()
            #  proxies = parse_body(r.content.decode("utf-8", "ignore"))
            #  info = ""
            #  ui = r.headers.get("subscription-userinfo", "")
            #  if ui:
                #  kv = dict(re.findall(r"(\w+)=([\d.]+)", ui))
                #  if "total" in kv:
                    #  used = float(kv.get("upload", 0)) + float(kv.get("download", 0))
                    #  info = f"余 {human_size(float(kv['total']) - used)}"
                #  if "expire" in kv and float(kv["expire"]) > 0:
                    #  info += " 到期 " + time.strftime("%Y-%m-%d", time.localtime(float(kv["expire"])))
            #  return (proxies, "ok", info) if proxies else ([], "解析失败(非clash格式?)", info)
        #  except requests.exceptions.SSLError:
            #  verify, err = False, "SSLError→忽略证书重试"
        #  except requests.RequestException as e:
            #  err = type(e).__name__
            #  time.sleep(1.5)
    #  return [], f"拉取失败: {err}", ""

def _make_sub_session(ua: str, cookie: str, engine: str):
    """订阅会话: 优先 curl_cffi(Chrome TLS/JA3 + HTTP2 指纹)。
    UA 保持 clash 系——机场按 UA 决定返回格式, 用浏览器 UA 会拿到网页而不是节点。"""
    if engine in ("auto", "cffi"):
        try:
            from curl_cffi import requests as creq
            s = creq.Session(impersonate="chrome")
            s.headers.update({"User-Agent": ua, "Accept": "*/*",
                              "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
            if cookie:
                s.headers["Cookie"] = cookie
            return s, "curl_cffi"
        except ImportError:
            if engine == "cffi":
                raise RuntimeError("--sub-engine cffi 需要: pip install curl_cffi")
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": ua, "Accept": "*/*",
                      "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    if cookie:
        s.headers["Cookie"] = cookie
    return s, "requests"


def fetch_sub(sub: Sub, ua: str, cookie: str = "", engine: str = "auto") -> tuple[list[dict], str, str]:
    sess, eng = _make_sub_session(ua, cookie, engine)
    verify, err = True, ""
    try:
        for attempt in range(3):
            try:
                r = sess.get(sub.url, timeout=30, verify=verify, allow_redirects=True)
                if r.status_code in (403, 429, 503):          # 风控信号 → 退避, 绝不硬撞
                    try:
                        wait = float(r.headers.get("Retry-After", "") or 0)
                    except ValueError:
                        wait = 0.0
                    wait = max(wait, 6.0 * (attempt + 1)) + random.uniform(0, 4)
                    err = f"HTTP {r.status_code}(风控/封禁)"
                    log(f"SUB-WAF {sub.name} [{eng}]: {r.status_code} → 退避 {wait:.0f}s")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                proxies = parse_body(r.content.decode("utf-8", "ignore"))
                info = ""
                ui = r.headers.get("subscription-userinfo", "")
                if ui:
                    kv = dict(re.findall(r"(\w+)=([\d.]+)", ui))
                    if "total" in kv:
                        used = float(kv.get("upload", 0)) + float(kv.get("download", 0))
                        info = f"余 {human_size(float(kv['total']) - used)}"
                    if "expire" in kv and float(kv["expire"]) > 0:
                        info += " 到期 " + time.strftime("%Y-%m-%d", time.localtime(float(kv["expire"])))
                return (proxies, "ok", info) if proxies else ([], "解析失败(非clash格式?)", info)
            except Exception as e:                            # 兼容 requests / curl_cffi 两套异常
                name, msg = type(e).__name__, str(e).lower()
                if "ssl" in name.lower() or "ssl" in msg or "certificate" in msg:
                    verify, err = False, "SSLError→忽略证书重试"
                else:
                    err = name
                time.sleep(2.0 + 3.0 * attempt + random.uniform(0, 2))
        return [], f"拉取失败: {err}", ""
    finally:
        try:
            sess.close()
        except Exception:
            pass

def load_subs_file(path: Path) -> list[Sub]:
    subs = []
    for ln, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            name, url = line.split("|", 1)
        else:
            parts = line.split(None, 1)
            if len(parts) != 2 or not parts[1].startswith("http"):
                raise ValueError(f"subs 第 {ln} 行格式错误(应为 机场名|URL)")
            name, url = parts
        name, url = name.strip(), url.strip()
        if not url.startswith("http"):
            raise ValueError(f"subs 第 {ln} 行 URL 无效")
        subs.append(Sub(name, url))
    return subs


def extract_from_clash(path: Path) -> list[Sub]:
    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    subs = []
    try:
        data = yaml.safe_load(text)
    except Exception:
        data = None
    if isinstance(data, dict) and isinstance(data.get("proxy-providers"), dict):
        for key, pv in data["proxy-providers"].items():
            if isinstance(pv, dict) and pv.get("url"):
                prefix = ((pv.get("override") or {}).get("additional-prefix") or "").rstrip("-")
                subs.append(Sub(prefix or str(key), pv["url"]))
        return subs
    lines, seen = text.splitlines(), set()
    for i, line in enumerate(lines):
        m = re.search(r'^\s*url:\s*["\']?(https?://[^"\'\s#]+)', line)
        if not m:
            continue
        url = m.group(1)
        if "generate_204" in url or url in seen:
            continue
        seen.add(url)
        name = None
        for j in range(i - 1, max(-1, i - 12), -1):
            km = re.match(r"^\s{0,8}([A-Za-z0-9_.\-]+):\s*$", lines[j])
            if km and km.group(1) not in ("proxy-providers", "override", "health-check", "dns"):
                name = km.group(1)
                break
        for j in range(i + 1, min(len(lines), i + 30)):
            pm = re.search(r'additional-prefix:\s*["\']([^"\']+)["\']', lines[j])
            if pm:
                name = pm.group(1).rstrip("-")
                break
            if re.search(r"^\s*url:", lines[j]):
                break
        subs.append(Sub(name or f"sub{len(subs)+1}", url))
    return subs


# ============================== 节点清洗 / 指纹 ==============================
def sanitize_proxy(p: dict) -> tuple[dict, str]:
    """剥离引用外部对象的字段 + 修复非法 REALITY short-id(避免内核整批拒绝)"""
    note = ""
    p = {k: v for k, v in p.items() if k not in STRIP_FIELDS}
    ro = p.get("reality-opts")
    if isinstance(ro, dict) and "short-id" in ro:
        sid = str(ro.get("short-id") or "")
        clean = re.sub(r"[^0-9a-fA-F]", "", sid)[:16].lower()
        if len(clean) % 2:
            clean = clean[:-1]
        if clean != sid:
            note = f"short-id 修正 {sid!r}→{clean!r}"
            ro = dict(ro)
            ro["short-id"] = clean
            p["reality-opts"] = ro
    return p, note


def fingerprint(provider: str, p: dict) -> str:
    cred = p.get("uuid") or p.get("password") or ""
    if p.get("type") == "ss":
        cred = f"{p.get('cipher','')}:{p.get('password','')}"
    #  raw = f"{provider}|{p.get('type')}|{p.get('server')}|{p.get('port')}|{cred}"
    raw = f"{provider}|{p.get('name')}|{p.get('type')}|{p.get('server')}|{p.get('port')}|{cred}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ============================== 状态 ==============================
def new_service() -> dict:
    return {"v": None, "at": None, "tries": 0, "detail": "", "quality": ""}


def new_node(provider, orig, name, proxy) -> dict:
    return {
        "provider": provider, "orig_name": orig, "name": name, "proxy": proxy,
        "present": True, "first_seen": now_str(), "last_seen": now_str(),
        "blocked": False, "blocked_reason": None, "blocked_at": None,
        "sealed": False, "tier": 0, "kernel_fail": 0, "last_google_ok": None,
        "region": {}, "services": {s: new_service() for s in SERVICES},
        "ip": {}, "ipqs": {}, "ipapi": {}, "abuseipdb": {},   # [IPQS]
        "reach": {"samples": 0, "google_ok": 0, "link_samples": 0, "link_ok": 0,
                  "recent": [], "consecutive_fail": 0, "delay_last": None,
                  "delay_ewma": None, "last_ok_at": None},
    }


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(st, dict) and "nodes" in st:
                st.setdefault("runs", [])
                st.setdefault("ip_cache", {})         # [IPQS] 按出口IP全局去重缓存
                for n in st["nodes"].values():        # 向后兼容补字段
                    for s in SERVICES:
                        n.setdefault("services", {}).setdefault(s, new_service())
                    n.setdefault("region", {})
                    n.setdefault("kernel_fail", 0)
                    n.setdefault("last_google_ok", None)
                    n.setdefault("sealed", False)
                    n.setdefault("ip", {})            # [IPQS]
                    n.setdefault("ipqs", {})          # [IPQS]
                    n.setdefault("ipapi", {})         # [IPQS]
                    n.setdefault("abuseipdb", {})     # [IPQS]
                return st
        except Exception as e:
            console.print(f"[yellow]state 读取失败({e})，将新建[/yellow]")
    return {"version": 3, "updated_at": None, "nodes": {}, "runs": [], "ip_cache": {}}


def save_state(st: dict, path: Path):
    st["updated_at"] = now_str()
    atomic_write(path, json.dumps(st, ensure_ascii=False, indent=1))


def tier_pass(n: dict, tier: int) -> bool:
    need = {1: T1, 2: T2, 3: T3}[tier]
    return all(n["services"][s]["v"] == 1 for s in need)


def finalize(n: dict):
    if n["blocked"]:
        n["tier"], n["sealed"] = 0, True
        return
    t = 0
    if tier_pass(n, 1):
        t = 1
        if tier_pass(n, 2):
            t = 2
            if tier_pass(n, 3):
                t = 3
    n["tier"] = t
    sv = n["services"]
    n["sealed"] = any(sv[s]["v"] == 0 for s in SERVICES) or \
                  all(sv[s]["v"] is not None for s in SERVICES)


def block(n: dict, reason: str):
    n["blocked"], n["blocked_reason"], n["blocked_at"] = True, reason, now_str()
    n["tier"], n["sealed"] = 0, True
    log(f"BLOCK {n['name']} :: {reason}")


def unlock_pending(n: dict) -> bool:
    return not n["blocked"] and not n["sealed"]


def provider_excluded(n: dict, args) -> bool:
    """--exclude-provider: 被排除机场的节点不输出/不进报告(测试与 state 照常)。"""
    return n.get("provider") in (getattr(args, "exclude_providers", None) or ())


def stability(n: dict, kind="google") -> Optional[float]:
    r = n["reach"]
    if kind == "google":
        return r["google_ok"] / r["samples"] if r["samples"] else None
    return r["link_ok"] / r["link_samples"] if r["link_samples"] else None


def stability_recent(n: dict) -> Optional[float]:
    rec = n["reach"]["recent"]
    return sum(rec) / len(rec) if rec else None


# ============================== mihomo ==============================
def find_mihomo(explicit: Optional[str]) -> tuple[Path, str]:
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    for nm in ("verge-mihomo", "mihomo", "clash-meta"):
        w = shutil.which(nm)
        if w:
            cands.append(Path(w))
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    la = os.environ.get("LOCALAPPDATA", "")
    roots = [r for r in (pf, pf86, os.path.join(la, "Programs") if la else "") if r]
    for root in roots:
        cands += [Path(root) / "Clash Verge" / "verge-mihomo.exe",
                  Path(root) / "Clash Verge" / "resources" / "verge-mihomo.exe"]
        try:
            cands += list(Path(root).glob("*/verge-mihomo.exe"))
            cands += list(Path(root).glob("*/resources/verge-mihomo.exe"))
        except OSError:
            pass
    for c in cands:
        if not c.is_file():
            continue
        try:
            out = subprocess.run([str(c), "-v"], capture_output=True, text=True,
                                 timeout=10, creationflags=CREATE_NO_WINDOW)
            return c, (out.stdout or out.stderr).strip().splitlines()[0]
        except Exception:
            continue
    raise FileNotFoundError("找不到 mihomo 内核，请用 --mihomo 指定 verge-mihomo.exe")


def port_free(p: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", p))
            return True
        except OSError:
            return False


def alloc_ports(n: int) -> list[int]:
    if n <= 0:
        return []
    for _ in range(80):
        base = random.randint(20000, 45000 - n)
        ports = list(range(base, base + n))
        if all(port_free(p) for p in ports):
            return ports
    raise RuntimeError(f"无法分配 {n} 个连续空闲端口")


class KernelStartError(Exception):
    pass


_LIVE: list["MihomoInstance"] = []


class MihomoInstance:
    def __init__(self, exe: Path, workdir: Path, cfg: Path, logf: Path):
        self.exe, self.workdir, self.cfg, self.logf = exe, workdir, cfg, logf
        self.proc: Optional[subprocess.Popen] = None
        self._fh = None

    def start(self):
        self.logf.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.logf, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(self.exe), "-d", str(self.workdir), "-f", str(self.cfg)],
            stdout=self._fh, stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
        _LIVE.append(self)

    def wait_ready(self, ctrl: int, secret: str, timeout=25):
        url = f"http://127.0.0.1:{ctrl}/version"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise KernelStartError(f"内核退出(code={self.proc.returncode}): {log_tail(self.logf, 6)}")
            try:
                with requests.Session() as s:
                    s.trust_env = False
                    r = s.get(url, headers={"Authorization": f"Bearer {secret}"}, timeout=2)
                if r.status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.3)
        raise KernelStartError(f"内核 {timeout}s 未就绪: {log_tail(self.logf, 6)}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        try:
            if self._fh:
                self._fh.close()
        except Exception:
            pass
        if self in _LIVE:
            _LIVE.remove(self)


def _atexit_cleanup():
    for inst in list(_LIVE):
        inst.stop()


atexit.register(_atexit_cleanup)


def build_config(nodes: list[dict], ctrl: int, secret: str, verbose: bool) -> dict:
    listeners = [{"name": f"in-{i:05d}", "type": "mixed", "listen": "127.0.0.1",
                  "port": n["port"], "proxy": n["name"]}
                 for i, n in enumerate(nodes) if n.get("port")]
    cfg = {
        "mode": "rule", "log-level": "info" if verbose else "warning",
        "ipv6": False, "allow-lan": False,
        "external-controller": f"127.0.0.1:{ctrl}", "secret": secret,
        "unified-delay": True, "find-process-mode": "off", "geo-auto-update": False,
        "dns": {"enable": False},
        "proxies": [n["proxy"] for n in nodes],
        "rules": ["MATCH,DIRECT"],
    }
    if listeners:
        cfg["listeners"] = listeners      # listener.proxy 优先级高于路由 → 钉死节点
    return cfg


#  def make_session(port: Optional[int]) -> requests.Session:
    #  s = requests.Session()
    #  s.trust_env = False
    #  s.headers["User-Agent"] = UA_BROWSER
    #  if port:
        #  s.proxies = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
    #  return s

def make_session(port: Optional[int]) -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({
        "User-Agent": UA_BROWSER,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    })
    if port:
        s.proxies = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
    return s


def http(sess, method, url, timeout, retries, **kw):
    last = ""
    for _ in range(retries + 1):
        try:
            r = sess.request(method, url, timeout=(5, timeout), allow_redirects=True, **kw)
            return r, None
        except requests.RequestException as e:
            last = type(e).__name__
            time.sleep(0.4)
    return None, last


def R(v=None, detail="", region=None, quality=None) -> dict:
    return {"v": v, "detail": detail, "region": region, "quality": quality}


# ============================== 探针 ==============================
def probe_delay(n: dict, ctrl: int, secret: str) -> tuple[bool, Optional[int]]:
    url = f"http://127.0.0.1:{ctrl}/proxies/{quote(n['name'], safe='')}/delay"
    try:
        with requests.Session() as s:
            s.trust_env = False
            r = s.get(url, params={"url": URL_DELAY, "timeout": 3000},
                      headers={"Authorization": f"Bearer {secret}"}, timeout=7)
        if r.status_code == 200:
            return True, int(r.json().get("delay", 0))
    except Exception:
        pass
    return False, None


def probe_apple(sess, args) -> dict:
    r, err = http(sess, "GET", URL_APPLE, args.timeout, args.retries)
    if err:
        return {"region": None, "reachable": False}
    txt = (r.text or "").strip()
    if re.fullmatch(r"[A-Za-z]{2}", txt):
        return {"region": txt.upper(), "reachable": True}
    return {"region": None, "reachable": r.status_code == 200}


def probe_chatgpt(sess, args) -> dict:
    """web 宽松：只看 unsupported_country；命中后用 favicon 403 复核(降低误杀)"""
    headers = {"accept": "*/*", "accept-language": "en-US,en;q=0.9",
               "authorization": "Bearer null", "content-type": "application/json",
               "origin": "https://platform.openai.com", "referer": "https://platform.openai.com/",
               "sec-ch-ua": SEC_CH_UA, "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'}
    r, err = http(sess, "GET", URL_OPENAI, args.timeout, args.retries, headers=headers)
    if err:
        return R(None, f"net:{err}")
    if "unsupported_country" in (r.text or "").lower():
        r2, err2 = http(sess, "GET", URL_OPENAI_FAVICON, args.timeout, 0,
                        headers={"referer": "https://chatgpt.com/", "accept": "*/*"})
        code = r2.status_code if r2 is not None else None
        if code == 403:
            return R(0, "unsupported_country + favicon403", quality="strong")
        return R(1, f"favicon={code} 撤销不支持判定", quality="recheck_ok")
    if r.status_code == 200:
        return R(1, "HTTP 200")
    return R(None, f"弱信号 HTTP {r.status_code}")


def probe_youtube(sess, args) -> dict:
    r, err = http(sess, "GET", URL_YT, args.timeout, args.retries,
                  headers={"accept-language": "en-US,en;q=0.9", "cookie": YT_COOKIE})
    if err:
        return R(None, f"net:{err}")
    t, low = r.text or "", (r.text or "").lower()
    m = re.search(r'"INNERTUBE_CONTEXT_GL"\s*:\s*"([A-Z]{2})"', t)
    region = m.group(1) if m else None
    if "www.google.cn" in t:
        return R(0, "送中(google.cn)", "CN", "strong")
    if "premium is not available in your country" in low:
        return R(0, "not available", region, "strong")
    if "ad-free" in low:
        return R(1, "ad-free", region)
    if "consent.youtube.com" in (r.url or ""):
        return R(None, "consent跳转", region)
    return R(None, "页面异常", region)


def probe_claude(sess, args) -> dict:
    r, err = http(sess, "GET", URL_CLAUDE, args.timeout, args.retries)
    if err:
        return R(None, f"net:{err}")
    final = (r.url or "").rstrip("/")
    if "app-unavailable-in-region" in final:
        return R(0, "app-unavailable-in-region", quality="strong")
    if final in ("https://claude.ai", "https://www.claude.ai"):
        return R(1, "claude.ai 200")
    return R(None, f"未知终点 {final[:60]}")


def probe_tiktok(sess, args) -> dict:
    """无强拒绝文案 → 只会是 1 或 null(未定论)"""
    r, err = http(sess, "GET", URL_TIKTOK, args.timeout, args.retries)
    if err:
        return R(None, f"net:{err}")
    t = r.text or ""
    if "Please wait..." in t:
        r2, err2 = http(sess, "GET", URL_TIKTOK + "explore", args.timeout, 0)
        if r2 is not None:
            t = r2.text or ""
    m = re.search(r'"region"\s*:\s*"([A-Za-z]{2})"', t)
    if m:
        return R(1, "region ok", m.group(1).upper(), "native")
    r3, err3 = http(sess, "GET", URL_TIKTOK, args.timeout, 0, headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip", "Accept-Language": "en"})
    if r3 is not None:
        m = re.search(r'"region"\s*:\s*"([A-Za-z]{2})"', r3.text or "")
        if m:
            return R(1, "region ok(2nd)", m.group(1).upper(), "idc")
    return R(None, "region 提取失败")


def probe_netflix(sess, args) -> dict:
    headers = {"accept-language": "en-US,en;q=0.9", "cookie": NF_COOKIE,
               "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    r1, e1 = http(sess, "GET", URL_NF_1, args.timeout, args.retries, headers=headers)
    r2, e2 = http(sess, "GET", URL_NF_2, args.timeout, args.retries, headers=headers)
    if e1 or e2:
        return R(None, f"net:{e1 or e2}")
    t1, t2 = r1.text or "", r2.text or ""
    region = None
    m = re.search(r'"requestCountry"\s*:\s*\{[^}]*?"id"\s*:\s*"([A-Z]{2})"', t1)
    if m:
        region = m.group(1)
    bad1, bad2 = "Oh no!" in t1, "Oh no!" in t2
    if bad1 and bad2:
        return R(0, "仅自制剧(Originals Only)", region, "originals_only")
    if not bad1 or not bad2:
        return R(1, "完整解锁", region, "full")
    return R(None, "页面异常", region)


def probe_disney(sess, args) -> dict:
    """bamgrid 三步 + disneyplus.com 跳转复核；强信号在 1/2 步与跳转即可判定"""
    h = {"authorization": f"Bearer {DISNEY_AUTH}", "content-type": "application/json; charset=UTF-8"}
    r1, e1 = http(sess, "POST", "https://disney.api.edge.bamgrid.com/devices", args.timeout,
                  args.retries, headers=h,
                  json={"deviceFamily": "browser", "applicationRuntime": "chrome",
                        "deviceProfile": "windows", "attributes": {}})
    if e1:
        return R(None, f"net:{e1}")
    t1 = r1.text or ""
    if "403 ERROR" in t1:
        return R(0, "IP Banned(403)", quality="strong")
    try:
        assertion = r1.json().get("assertion")
    except Exception:
        assertion = None
    if not assertion:
        return R(None, "assertion 缺失")
    r2, e2 = http(sess, "POST", "https://disney.api.edge.bamgrid.com/token", args.timeout,
                  args.retries, headers={"authorization": f"Bearer {DISNEY_AUTH}"},
                  data={"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                        "subject_token_type": "urn:bamtech:params:oauth:token-type:device",
                        "platform": "browser", "subject_token": assertion})
    if e2:
        return R(None, f"net:{e2}")
    t2 = r2.text or ""
    if "forbidden-location" in t2 or "403 ERROR" in t2:
        return R(0, "forbidden-location", quality="strong")
    r3, _ = http(sess, "GET", "https://disneyplus.com", args.timeout, 0)
    final = (r3.url if r3 is not None else "") or ""
    if re.search(r"preview|unavailable", final):
        return R(0, f"跳转 {final[:50]}", quality="strong")
    region, supported = None, None
    try:
        refresh = r2.json().get("refresh_token")
    except Exception:
        refresh = None
    if refresh:
        r4, _ = http(sess, "POST", "https://disney.api.edge.bamgrid.com/graph/v1/device/graphql",
                     args.timeout, 0,
                     headers={"authorization": DISNEY_AUTH, "content-type": "application/json"},
                     json={"query": "query { session { inSupportedLocation location { countryCode } } }",
                           "variables": {}})
        if r4 is not None:
            t4 = r4.text or ""
            m = re.search(r'"countryCode"\s*:\s*"([A-Z]{2})"', t4)
            region = m.group(1) if m else None
            m2 = re.search(r'"inSupportedLocation"\s*:\s*(true|false)', t4)
            supported = (m2.group(1) == "true") if m2 else None
    if region == "JP":
        return R(1, "JP", region)
    if supported is True:
        return R(1, "inSupportedLocation", region)
    if supported is False:
        return R(0, "Coming Soon", region, "pending")
    return R(None, "graphql 未定论", region)


def probe_primevideo(sess, args) -> dict:
    r, err = http(sess, "GET", URL_PV, args.timeout, args.retries)
    if err:
        return R(None, f"net:{err}")
    t = r.text or ""
    if "isServiceRestricted" in t:
        return R(0, "isServiceRestricted", quality="strong")
    m = re.search(r'"currentTerritory"\s*:\s*"([A-Z]{2})"', t)
    if m:
        return R(1, "currentTerritory", m.group(1))
    return R(None, "页面异常")


PROBES = [("chatgpt", probe_chatgpt), ("youtube", probe_youtube),
          ("claude", probe_claude), ("tiktok", probe_tiktok),
          ("netflix", probe_netflix), ("disney", probe_disney),
          ("primevideo", probe_primevideo)]


# ============================== IP 纯净度独立单元 ==============================
# [IPQS] 自包含单元: 出口IP探测(防直连泄漏/飘移检测) + IPQS/ipapi.is/AbuseIPDB 多源查询
_IP_API_LOCK = Lock()
_IP_RUN_CTX: dict = {
    "local_ip": None,
    "cache": {},
    "exhausted": {},
    "refreshed": set(),
    "last_at": {},
}
_EXHAUST_KWS = ("invalid", "insufficient", "exceeded", "limit", "quota",
                "credit", "unauthorized", "forbidden", "expired", "key")


def reset_ip_run_ctx(st: dict, args):
    """每轮开始前重置内存态并探测一次本机直连 IP, 防止代理掉线走 DIRECT 误判。"""
    _IP_RUN_CTX["cache"] = st.setdefault("ip_cache", {})
    _IP_RUN_CTX["exhausted"] = {}
    _IP_RUN_CTX["refreshed"] = set()
    _IP_RUN_CTX["last_at"] = {}
    _IP_RUN_CTX["local_ip"] = detect_local_ip(args)


def _valid_public_ip(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        obj = ipaddress.ip_address(str(raw).strip())
    except ValueError:
        return None
    if (obj.is_private or obj.is_loopback or obj.is_link_local
            or obj.is_multicast or obj.is_reserved or obj.is_unspecified):
        return None
    return str(obj)


def detect_local_ip(args) -> Optional[str]:
    """直连探测本机公网 IP, 作为代理泄漏防呆基线。"""
    with make_session(None) as ds:
        res = detect_exit_ip(ds, args, check_leak=False)
        return res.get("addr") if res.get("ok") else None


def detect_exit_ip(sess, args, check_leak: bool = True) -> dict:
    """多源轮询获取节点真实出口 IP: cloudflare trace -> ipify -> ipinfo -> ip-api。"""
    sources = [
        ("cloudflare", "https://www.cloudflare.com/cdn-cgi/trace"),
        ("ipify", "https://api.ipify.org?format=json"),
        ("ipinfo", "https://ipinfo.io/json"),
        ("ip-api", "http://ip-api.com/json/"),
    ]
    to = min(float(getattr(args, "timeout", 8) or 8), 6.0)
    last_err = "unknown"
    local_ip = _IP_RUN_CTX.get("local_ip") if check_leak else None
    for src, url in sources:
        r, err = http(sess, "GET", url, to, 0)
        if err:
            last_err = f"{src}:{err}"
            continue
        if r.status_code != 200:
            last_err = f"{src}:HTTP{r.status_code}"
            continue
        raw_ip, cf_loc = None, ""
        if src == "cloudflare":
            txt = r.text or ""
            m_ip = re.search(r"(?m)^ip=([^\s]+)", txt)
            m_loc = re.search(r"(?m)^loc=([A-Za-z]{2})\b", txt)
            raw_ip = m_ip.group(1) if m_ip else None
            cf_loc = m_loc.group(1).upper() if m_loc else ""
        else:
            try:
                j = r.json()
            except Exception:
                last_err = f"{src}:bad_json"
                continue
            raw_ip = j.get("query") if src == "ip-api" else j.get("ip")
        ip_addr = _valid_public_ip(raw_ip)
        if not ip_addr:
            last_err = f"{src}:invalid_ip"
            continue
        if local_ip and ip_addr == local_ip:
            last_err = "直连泄漏(exit_ip==local_ip)"
            log(f"IP-LEAK {src}: {ip_addr}")
            return {"ok": False, "addr": None, "source": src, "cf_loc": "",
                    "at": now_str(), "err": last_err}
        return {"ok": True, "addr": ip_addr, "source": src, "cf_loc": cf_loc,
                "at": now_str(), "err": None}
    return {"ok": False, "addr": None, "source": "", "cf_loc": "",
            "at": now_str(), "err": last_err}


def _update_node_ip(n: dict, res: dict):
    prev = dict(n.get("ip") or {})
    if not res.get("ok"):
        prev["err"] = res.get("err")
        prev["at"] = res.get("at")
        n["ip"] = prev
        return
    hist = list(prev.get("history") or [])
    changes = int(prev.get("changes") or 0)
    old_addr, new_addr = prev.get("addr"), res["addr"]
    if old_addr and old_addr != new_addr:
        changes += 1
    if not hist or hist[-1] != new_addr:
        hist.append(new_addr)
        hist = hist[-10:]
    n["ip"] = {
        "addr": new_addr, "source": res["source"], "cf_loc": res.get("cf_loc") or "",
        "at": res["at"], "history": hist, "changes": changes, "err": None,
    }


def _parse_abuser_score(val) -> tuple[Optional[float], str]:
    if val is None or val == "":
        return None, ""
    if isinstance(val, (int, float)):
        return float(val), ""
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(?:\(([^)]+)\))?", str(val))
    if not m:
        return None, str(val).strip()
    return float(m.group(1)), (m.group(2) or "").strip()


def _is_exhausted_msg(status: int, msg: str) -> bool:
    if status in (401, 402, 403, 429):
        return True
    low = (msg or "").lower()
    return any(kw in low for kw in _EXHAUST_KWS)


def _fetch_ipqs(ip: str, args, ds) -> tuple[dict, bool]:
    url = f"https://www.ipqualityscore.com/api/json/ip/{args.ipqs_key}/{ip}"
    params = {"strictness": args.ipqs_strictness,
              "allow_public_access_points": "true", "lighter_penalties": "true"}
    try:
        r = ds.get(url, params=params, timeout=(5, max(args.timeout, 10)))
    except requests.RequestException as e:
        return {"ok": False, "ip": ip, "at": now_str(), "err": type(e).__name__}, False
    try:
        j = r.json()
    except Exception:
        return {"ok": False, "ip": ip, "at": now_str(), "err": f"HTTP{r.status_code}:bad_json"}, r.status_code in (401, 402, 403, 429)
    if r.status_code != 200 or not j.get("success"):
        msg = str(j.get("message") or f"HTTP{r.status_code}")
        return {"ok": False, "ip": ip, "at": now_str(), "err": msg}, _is_exhausted_msg(r.status_code, msg)
    return {
        "ok": True, "ip": ip, "at": now_str(), "err": None,
        "strictness": args.ipqs_strictness,
        "fraud_score": j.get("fraud_score"),
        "proxy": bool(j.get("proxy")),
        "vpn": bool(j.get("vpn")),
        "tor": bool(j.get("tor")),
        "active_vpn": bool(j.get("active_vpn")),
        "active_tor": bool(j.get("active_tor")),
        "recent_abuse": bool(j.get("recent_abuse")),
        "bot_status": bool(j.get("bot_status")),
        "is_crawler": bool(j.get("is_crawler")),
        "mobile": bool(j.get("mobile")),
        "connection_type": j.get("connection_type") or "",
        "country_code": (j.get("country_code") or "").upper(),
        "region": j.get("region") or "",
        "city": j.get("city") or "",
        "ISP": j.get("ISP") or "",
        "ASN": j.get("ASN"),
        "organization": j.get("organization") or "",
        "host": j.get("host") or "",
    }, False


def _fetch_ipapi(ip: str, args, ds) -> tuple[dict, bool]:
    try:
        r = ds.get("https://api.ipapi.is/", params={"q": ip, "key": args.ipapi_key},
                   timeout=(5, max(args.timeout, 10)))
    except requests.RequestException as e:
        return {"ok": False, "ip": ip, "at": now_str(), "err": type(e).__name__}, False
    try:
        j = r.json()
    except Exception:
        return {"ok": False, "ip": ip, "at": now_str(), "err": f"HTTP{r.status_code}:bad_json"}, r.status_code in (401, 402, 403, 429)
    if r.status_code != 200 or j.get("error"):
        msg = str(j.get("error") or f"HTTP{r.status_code}")
        return {"ok": False, "ip": ip, "at": now_str(), "err": msg}, _is_exhausted_msg(r.status_code, msg)
    comp = j.get("company") if isinstance(j.get("company"), dict) else {}
    asn = j.get("asn") if isinstance(j.get("asn"), dict) else {}
    loc = j.get("location") if isinstance(j.get("location"), dict) else {}
    co_score, co_label = _parse_abuser_score(comp.get("abuser_score"))
    asn_score, asn_label = _parse_abuser_score(asn.get("abuser_score"))
    return {
        "ok": True, "ip": ip, "at": now_str(), "err": None,
        "is_datacenter": bool(j.get("is_datacenter")),
        "is_vpn": bool(j.get("is_vpn")),
        "is_proxy": bool(j.get("is_proxy")),
        "is_tor": bool(j.get("is_tor")),
        "is_abuser": bool(j.get("is_abuser")),
        "is_mobile": bool(j.get("is_mobile")),
        "is_crawler": bool(j.get("is_crawler")),
        "company_name": comp.get("name") or "",
        "company_type": (comp.get("type") or "").lower(),
        "company_abuser_score": co_score,
        "company_abuser_label": co_label,
        "asn": asn.get("asn"),
        "asn_org": asn.get("org") or "",
        "asn_type": (asn.get("type") or "").lower(),
        "asn_abuser_score": asn_score,
        "asn_abuser_label": asn_label,
        "country_code": (loc.get("country_code") or "").upper(),
    }, False


def _fetch_abuseipdb(ip: str, args, ds) -> tuple[dict, bool]:
    headers = {"Key": args.abuseipdb_key, "Accept": "application/json"}
    params = {"ipAddress": ip, "maxAgeInDays": 90}
    try:
        r = ds.get("https://api.abuseipdb.com/api/v2/check", headers=headers,
                   params=params, timeout=(5, max(args.timeout, 10)))
    except requests.RequestException as e:
        return {"ok": False, "ip": ip, "at": now_str(), "err": type(e).__name__}, False
    try:
        j = r.json()
    except Exception:
        return {"ok": False, "ip": ip, "at": now_str(), "err": f"HTTP{r.status_code}:bad_json"}, r.status_code in (401, 402, 403, 429)
    if r.status_code != 200 or j.get("errors"):
        errs = j.get("errors") or []
        msg = "; ".join(str(x.get("detail") or x) for x in errs if isinstance(x, dict)) or f"HTTP{r.status_code}"
        return {"ok": False, "ip": ip, "at": now_str(), "err": msg}, _is_exhausted_msg(r.status_code, msg)
    d = j.get("data") if isinstance(j.get("data"), dict) else {}
    return {
        "ok": True, "ip": ip, "at": now_str(), "err": None,
        "abuse_score": d.get("abuseConfidenceScore"),
        "total_reports": d.get("totalReports", 0),
        "distinct_users": d.get("numDistinctUsers", 0),
        "last_reported_at": d.get("lastReportedAt") or "",
        "usage_type": d.get("usageType") or "",
        "isp": d.get("isp") or "",
        "domain": d.get("domain") or "",
        "is_tor": bool(d.get("isTor")),
        "country_code": (d.get("countryCode") or "").upper(),
    }, False


_IP_BACKENDS = {
    "ipqs": (_fetch_ipqs, lambda a: bool(getattr(a, "ipqs_key", None))),
    "ipapi": (_fetch_ipapi, lambda a: bool(getattr(a, "ipapi_key", None))),
    "abuseipdb": (_fetch_abuseipdb, lambda a: bool(getattr(a, "abuseipdb_key", None))),
}


def _cached_ip_lookup(svc: str, ip: str, args, interval: float) -> Optional[dict]:
    """直连查询某项 IP 情报: 全局 Lock 串行 + 间隔限速 + 按 IP 全局缓存 + 本轮止损。"""
    fetcher, _ = _IP_BACKENDS[svc]
    with _IP_API_LOCK:
        cache = _IP_RUN_CTX["cache"].setdefault(ip, {})
        cached = cache.get(svc)
        refreshed = _IP_RUN_CTX["refreshed"]
        reuse = (isinstance(cached, dict) and cached.get("ok") is True
                 and (not getattr(args, "ipqs_refresh", False) or (svc, ip) in refreshed))
        if reuse:
            return dict(cached)
        if svc in _IP_RUN_CTX["exhausted"]:
            return None
        last = _IP_RUN_CTX["last_at"].get(svc, 0.0)
        wait = interval - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        with make_session(None) as ds:
            res, fatal = fetcher(ip, args, ds)
        _IP_RUN_CTX["last_at"][svc] = time.time()
        if res.get("ok"):
            cache[svc] = res
            refreshed.add((svc, ip))
            log(f"{svc.upper()} {ip} -> ok")
        else:
            log(f"{svc.upper()}-ERR {ip}: {res.get('err')}")
            if fatal:
                _IP_RUN_CTX["exhausted"][svc] = res.get("err") or "exhausted"
                console.print(f"[yellow]⚠ {svc} 止损(密钥/配额异常): {res.get('err')}[/yellow]")
        return res


def ipqs_eligible(n: dict, args) -> bool:
    return not n.get("blocked") and n.get("tier", 0) >= getattr(args, "ipqs_min_tier", 0)


def ipqs_pending(n: dict, args) -> bool:
    if not ipqs_eligible(n, args):
        return False
    if getattr(args, "ipqs_refresh", False):
        return True
    addr = (n.get("ip") or {}).get("addr")
    if not addr:
        return True
    for svc, (_, has_key) in _IP_BACKENDS.items():
        if has_key(args):
            cur = n.get(svc) or {}
            if not cur.get("ok") or cur.get("ip") != addr:
                return True
    return False


def needs_exit_ip_probe(n: dict, args) -> bool:
    return bool(getattr(args, "ipqs", False) and ipqs_pending(n, args)
                and (getattr(args, "ipqs_refresh", False) or not (n.get("ip") or {}).get("addr")))


def maybe_ipqs_block(n: dict, args) -> bool:
    lim = getattr(args, "ipqs_block_above", None)
    if lim is None or n.get("blocked"):
        return False
    ipqs = n.get("ipqs") or {}
    if not ipqs.get("ok"):
        return False
    fs = ipqs.get("fraud_score")
    tor = bool(ipqs.get("tor") or ipqs.get("active_tor"))
    if tor or (fs is not None and fs >= lim):
        block(n, f"IPQS高危(score={fs}, tor={tor})")
        return True
    return False


def do_ip_purity(n: dict, sess, args):
    """独立单元主入口: 取出口 IP(如需) + 直连补齐 IPQS/ipapi.is/AbuseIPDB。"""
    if not ipqs_eligible(n, args):
        return
    need_probe = getattr(args, "ipqs_refresh", False) or not (n.get("ip") or {}).get("addr")
    if need_probe:
        if not n.get("port"):
            return
        res = detect_exit_ip(sess, args, check_leak=True)
        _update_node_ip(n, res)
        if not res.get("ok"):
            log(f"EXIT-IP-FAIL {n['name']}: {res.get('err')}")
            return
        log(f"EXIT-IP {n['name']} -> {res['addr']} ({res['source']} loc={res.get('cf_loc') or '-'})")

    ip_addr = (n.get("ip") or {}).get("addr")
    if not ip_addr:
        return
    for svc, (_, has_key) in _IP_BACKENDS.items():
        if not has_key(args):
            continue
        cur = n.get(svc) or {}
        if cur.get("ok") and cur.get("ip") == ip_addr and not getattr(args, "ipqs_refresh", False):
            continue
        gap = args.ipqs_interval if svc == "ipqs" else min(float(args.ipqs_interval), 0.35)
        res = _cached_ip_lookup(svc, ip_addr, args, gap)
        if res is not None and (res.get("ok") or not cur.get("ok")):
            n[svc] = res
    maybe_ipqs_block(n, args)


def compute_purity(n: dict) -> dict:
    """汇总 IPQS + ipapi.is + AbuseIPDB + 出口稳定性，输出综合评级(S/A/B/C/D)与告警标签。"""
    ip_info = n.get("ip") or {}
    ipqs = n.get("ipqs") or {}
    ipapi = n.get("ipapi") or {}
    abuse = n.get("abuseipdb") or {}
    iq_ok = bool(ipqs.get("ok"))
    ia_ok = bool(ipapi.get("ok"))
    ab_ok = bool(abuse.get("ok"))
    if not (iq_ok or ia_ok or ab_ok):
        return {"grade": "", "flags": []}

    flags: list[str] = []
    changes = int(ip_info.get("changes") or 0)
    if changes > 0:
        flags.append(f"出口飘移×{changes}")
    cf_loc = (ip_info.get("cf_loc") or "").upper()
    yt_reg = (n.get("region", {}).get("youtube") or "").upper()
    split_route = bool(cf_loc and yt_reg and cf_loc != yt_reg)
    if split_route:
        flags.append(f"分流({cf_loc}/{yt_reg})")

    fs = ipqs.get("fraud_score") if iq_ok else None
    iq_tor = bool(iq_ok and (ipqs.get("tor") or ipqs.get("active_tor")))
    iq_vpn = bool(iq_ok and (ipqs.get("vpn") or ipqs.get("active_vpn")))
    iq_proxy = bool(iq_ok and ipqs.get("proxy"))
    iq_abuse = bool(iq_ok and ipqs.get("recent_abuse"))
    iq_bot = bool(iq_ok and ipqs.get("bot_status"))
    if iq_ok:
        if fs is not None and fs >= 26:
            flags.append(f"IPQS:{fs}")
        for cond, lbl in ((iq_tor, "tor"), (iq_vpn, "vpn"), (iq_proxy, "proxy"),
                          (iq_abuse, "abuse"), (iq_bot, "bot")):
            if cond:
                flags.append(lbl)

    max_ab = None
    ia_isp_clean = False
    if ia_ok:
        asn_ab, co_ab = ipapi.get("asn_abuser_score"), ipapi.get("company_abuser_score")
        vals = [x for x in (asn_ab, co_ab) if x is not None]
        max_ab = max(vals) if vals else None
        for cond, lbl in ((ipapi.get("is_tor"), "ipapi:tor"),
                          (ipapi.get("is_abuser"), "ipapi:abuser"),
                          (ipapi.get("is_vpn"), "ipapi:vpn"),
                          (ipapi.get("is_proxy"), "ipapi:proxy")):
            if cond:
                flags.append(lbl)
        if max_ab is not None and max_ab >= 0.1:
            flags.append(f"ASN高滥用({max_ab:.2f})")
        elif max_ab is not None and max_ab >= 0.02:
            flags.append(f"ASN中滥用({max_ab:.3f})")
        if ipapi.get("company_type") == "isp" and not ipapi.get("is_datacenter"):
            ia_isp_clean = True
            flags.append("ISP原生")

    ab_score = abuse.get("abuse_score") if ab_ok else None
    ab_rep = int(abuse.get("total_reports") or 0) if ab_ok else 0
    if ab_ok:
        if ab_score is not None and ab_score > 0:
            flags.append(f"Abuse:{ab_score}%")
        if ab_rep > 0:
            flags.append(f"举报×{ab_rep}")

    fs_v = fs if fs is not None else 0
    ab_v = ab_score if ab_score is not None else 0
    if (iq_ok and (iq_tor or fs_v >= 75 or (iq_abuse and fs_v >= 50))) \
            or (ab_ok and (ab_v >= 25 or ab_rep >= 10)) \
            or (ia_ok and (ipapi.get("is_tor") or (ipapi.get("is_abuser") and (max_ab or 0) >= 0.1))):
        grade = "D"
    elif (iq_ok and (fs_v >= 50 or iq_abuse or iq_bot)) \
            or (ia_ok and (ipapi.get("is_abuser") or (max_ab is not None and max_ab >= 0.1))) \
            or (ab_ok and (ab_v > 0 or ab_rep >= 3)) \
            or (iq_vpn and ia_ok and ipapi.get("is_vpn")):
        grade = "C"
    elif (iq_ok and (fs_v >= 26 or iq_vpn or iq_proxy)) \
            or (ia_ok and (ipapi.get("is_vpn") or ipapi.get("is_proxy") or (max_ab is not None and max_ab >= 0.02))) \
            or (ab_ok and ab_rep > 0) or changes > 0 or split_route:
        grade = "B"
    elif (not iq_ok or fs_v <= 5) and n.get("tier", 0) >= 2 \
            and (not ia_ok or ia_isp_clean or (max_ab is not None and max_ab < 0.005)):
        grade = "S"
    else:
        grade = "A"
    return {"grade": grade, "flags": flags}


def any_ip_purity_data(st: dict) -> bool:
    return any((n.get("ip") or {}).get("addr") or (n.get("ipqs") or {}).get("ok")
               or (n.get("ipapi") or {}).get("ok") or (n.get("abuseipdb") or {}).get("ok")
               for n in st.get("nodes", {}).values())


def show_ipqs(st: dict, args):
    nodes = [n for n in st["nodes"].values()
             if n.get("present") and not provider_excluded(n, args)]
    tested = [n for n in nodes if (n.get("ip") or {}).get("addr")
              or (n.get("ipqs") or {}).get("ok")
              or (n.get("ipapi") or {}).get("ok")
              or (n.get("abuseipdb") or {}).get("ok")]
    if not tested:
        if getattr(args, "ipqs", False):
            console.print("[dim]暂无 IP 纯净度数据[/dim]")
        return

    uniq_ips = {(n.get("ip") or {}).get("addr") for n in tested if (n.get("ip") or {}).get("addr")}
    g_cnt = {g: 0 for g in ("S", "A", "B", "C", "D")}
    for n in tested:
        g = compute_purity(n)["grade"]
        if g in g_cnt:
            g_cnt[g] += 1
    console.print(f"[cyan]IP 纯净度汇总[/cyan]：已测 {len(tested)} 节点 ({len(uniq_ips)} 唯一出口IP) | "
                  + " ".join(f"{k}:{v}" for k, v in g_cnt.items()))

    rank_map = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4, "": 5}
    scored = []
    for n in tested:
        pu = compute_purity(n)
        iq = n.get("ipqs") or {}
        ia = n.get("ipapi") or {}
        ab = n.get("abuseipdb") or {}
        key = (
            rank_map.get(pu["grade"], 5),
            -n.get("tier", 0),
            iq.get("fraud_score") if iq.get("ok") and iq.get("fraud_score") is not None else 999,
            ab.get("abuse_score") if ab.get("ok") and ab.get("abuse_score") is not None else 999,
            ia.get("asn_abuser_score") if ia.get("ok") and ia.get("asn_abuser_score") is not None else 999,
            n["name"],
        )
        scored.append((key, n, pu))
    scored.sort(key=lambda x: x[0])

    t = Table(title="Top 25 IP 纯净度排行 (评级↓, Tier↓, IPQS↑)", box=box.SIMPLE)
    for c in ("节点", "档", "地区", "出口IP", "级", "IPQS", "ASN滥用", "Abuse", "备注"):
        t.add_column(c, no_wrap=(c in ("档", "地区", "出口IP", "级", "IPQS", "Abuse")))
    for _, n, pu in scored[:25]:
        ip_info = n.get("ip") or {}
        iq = n.get("ipqs") or {}
        ia = n.get("ipapi") or {}
        ab = n.get("abuseipdb") or {}
        yt_r = n.get("region", {}).get("youtube") or "-"
        cf_r = ip_info.get("cf_loc") or "-"
        iq_s = str(iq.get("fraud_score")) if iq.get("ok") else "-"
        ia_s = (f"{ia['asn_abuser_score']:.3f}({ia.get('company_type') or '-'})"
                if ia.get("ok") and ia.get("asn_abuser_score") is not None
                else (ia.get("company_type") or "-" if ia.get("ok") else "-"))
        ab_s = (f"{ab.get('abuse_score')}%/{ab.get('total_reports', 0)}"
                if ab.get("ok") else "-")
        t.add_row(
            n["name"][:40], f"T{n['tier']}", f"{yt_r}/{cf_r}",
            ip_info.get("addr") or "-", pu["grade"] or "-",
            iq_s, ia_s, ab_s, ", ".join(pu["flags"])[:36] or "-",
        )
    console.print(t)


# ============================== 解锁状态机(增量) ==============================
def do_unlock(n: dict, sess, args):
    sv = n["services"]
    if not n["region"].get("apple"):
        a = probe_apple(sess, args)
        if a["region"]:
            n["region"]["apple"] = a["region"]
            if a["region"] in args.blocked_regions:
                block(n, f"Apple地区={a['region']}")
                return
    for name, fn in PROBES:
        if sv[name]["v"] is not None:
            continue                                   # 已定论 → 永不重测
        tier = TIER_OF[name]
        if tier >= 2 and not tier_pass(n, 1):
            break                                      # 下层未通 → 等下一轮
        if tier == 3 and not tier_pass(n, 2):
            break
        r = fn(sess, args)
        time.sleep(random.uniform(0.4, 1.2))   # 服务探测间随机抖动
        s = sv[name]
        s["tries"] += 1
        s["at"] = now_str()
        s["detail"] = r["detail"]
        if r["region"]:
            n["region"][name] = r["region"]
        if r["quality"]:
            s["quality"] = r["quality"]
        if r["v"] is not None:
            s["v"] = r["v"]
        elif s["tries"] >= args.max_attempts:
            s["v"], s["quality"] = 0, "gave_up"
            s["detail"] = f"止损({args.max_attempts}次未定论)"
        log(f"UNLOCK {n['name']} {name} -> {s['v']} ({s['detail']})")
        if s["v"] == 0:
            if tier == 1:
                block(n, f"{name}: {s['detail']}")
                return
            break                                      # T2/T3 强0 → 封顶封盘
    finalize(n)


def health_sample(n: dict, sess, args, ctrl: int, secret: str):
    ok, delay = probe_delay(n, ctrl, secret)
    r = n["reach"]
    r["samples"] += 1
    r["recent"].append(1 if ok else 0)
    if len(r["recent"]) > RECENT_WINDOW:
        del r["recent"][0]
    n["last_google_ok"] = ok
    if ok:
        r["google_ok"] += 1
        r["link_samples"] += 1
        r["link_ok"] += 1                              # Google 通 ⇒ 链路必然通
        r["consecutive_fail"] = 0
        r["last_ok_at"] = now_str()
        r["delay_last"] = delay
        r["delay_ewma"] = delay if r["delay_ewma"] is None else round(0.3 * delay + 0.7 * r["delay_ewma"])
        return
    r["consecutive_fail"] += 1
    if not n.get("port"):
        return                                         # 本轮无 listener, 下轮再补 Apple
    a = probe_apple(sess, args)
    r["link_samples"] += 1

    #  if a["reachable"]:
        #  r["link_ok"] += 1
        #  if a["region"]:
            #  n["region"]["apple"] = a["region"]
            #  if a["region"] in args.blocked_regions:
                #  block(n, f"Apple地区={a['region']}(Google不可达)")
    #  else:
        #  log(f"DEAD {n['name']} (204+Apple 均不可达)")
#  
    if a["reachable"]:
        r["link_ok"] += 1
        if a["region"]:
            n["region"]["apple"] = a["region"]
            if a["region"] in args.blocked_regions and not args.stability_only:
                block(n, f"Apple地区={a['region']}(Google不可达)")
    else:
        log(f"DEAD {n['name']} (204+Apple 均不可达)")


#  def worker(n: dict, args, ctrl: int, secret: str) -> dict:
    #  sess = make_session(n.get("port"))
    #  try:
        #  if unlock_pending(n) and n.get("port"):
            #  do_unlock(n, sess, args)
        #  if not n["blocked"] and not args.no_stability_sample:
            #  health_sample(n, sess, args, ctrl, secret)
    #  except Exception as e:
        #  log(f"WORKER-ERR {n['name']}: {type(e).__name__} {e}")
    #  finally:
        #  sess.close()
        #  n["port"] = None
    #  return n
#  


def worker(n: dict, args, ctrl: int, secret: str) -> dict:
    sess = make_session(n.get("port"))
    try:
        if args.stability_only:
            health_sample(n, sess, args, ctrl, secret)
            if getattr(args, "ipqs", False) and ipqs_pending(n, args):   # [IPQS]
                do_ip_purity(n, sess, args)
        elif getattr(args, "ipqs_only", False):                           # [IPQS]
            if ipqs_pending(n, args):
                do_ip_purity(n, sess, args)
        else:
            if unlock_pending(n) and n.get("port"):
                do_unlock(n, sess, args)
            if not n["blocked"] and not args.no_stability_sample:
                health_sample(n, sess, args, ctrl, secret)
            if getattr(args, "ipqs", False) and ipqs_pending(n, args):   # [IPQS]
                do_ip_purity(n, sess, args)
    except Exception as e:
        log(f"WORKER-ERR {n['name']}: {type(e).__name__} {e}")
    finally:
        sess.close()
        n["port"] = None
    return n

# ============================== 批次运行 ==============================
#  def needs_listener(n: dict) -> bool:
    #  return unlock_pending(n) or n.get("last_google_ok") is False
def needs_listener(n: dict, args) -> bool:
    if args.stability_only:
        # 204 走控制API不需要listener；只有上轮失败的节点才需要listener补Apple区分死/送中
        return n.get("last_google_ok") is False or needs_exit_ip_probe(n, args)   # [IPQS]
    if getattr(args, "ipqs_only", False):                                          # [IPQS]
        return needs_exit_ip_probe(n, args)
    return (unlock_pending(n) or n.get("last_google_ok") is False
            or needs_exit_ip_probe(n, args))                                       # [IPQS]

def run_batch_once(nodes: list[dict], args, exe: Path, workdir: Path, on_done):
    #  need = [n for n in nodes if needs_listener(n)]
    need = [n for n in nodes if needs_listener(n, args)]
    if getattr(args, "ipqs_only", False) and not need:    # [IPQS] 已有出口IP仅补直连查询时免起内核
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = [ex.submit(worker, n, args, 0, "") for n in nodes]
            for f in as_completed(futs):
                on_done(f.result())
        return
    ports = alloc_ports(len(need) + 1)
    ctrl, secret = ports[0], rand_secret()
    for n, p in zip(need, ports[1:]):
        n["port"] = p
    sub = workdir / f"b{random.randint(0, 99999):05d}"
    sub.mkdir(parents=True, exist_ok=True)
    cfg, logf = sub / "config.yaml", sub / "mihomo.log"
    cfg.write_text(yaml.safe_dump(build_config(nodes, ctrl, secret, args.verbose),
                                  allow_unicode=True, sort_keys=False, width=4096), encoding="utf-8")
    inst = MihomoInstance(exe, sub, cfg, logf)
    inst.start()
    try:
        inst.wait_ready(ctrl, secret)
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = [ex.submit(worker, n, args, ctrl, secret) for n in nodes]
            for f in as_completed(futs):
                on_done(f.result())
    finally:
        inst.stop()
        for n in nodes:
            n["port"] = None


def run_batch(nodes: list[dict], args, exe: Path, workdir: Path, on_done, budget: list[int]):
    try:
        run_batch_once(nodes, args, exe, workdir, on_done)
    except KernelStartError as e:
        if len(nodes) == 1 or budget[0] <= 0:
            for n in nodes:
                n["kernel_fail"] += 1
                #  if n["kernel_fail"] >= 3:
                    #  block(n, "内核连续拒绝(协议/字段非法)")
                if n["kernel_fail"] >= 3:
                    block(n, "内核连续拒绝(协议/字段非法)")

                on_done(n)
            console.print(f"[yellow]⚠ {len(nodes)} 个节点内核拒绝[/yellow] {str(e).splitlines()[-1][:110]}")
            log(f"KERNEL-REJECT {len(nodes)} nodes: {e}")
            return
        budget[0] -= 1
        mid = len(nodes) // 2
        run_batch(nodes[:mid], args, exe, workdir, on_done, budget)
        run_batch(nodes[mid:], args, exe, workdir, on_done, budget)


# ============================== 输出 ==============================
def apply_filters(pool: list[dict], args) -> list[dict]:
    """★ 筛选算法钩子 —— 当前为空实现(按要求)。
    后续可在此加入：稳定性门槛 / 地区白名单 / 延迟上限 / 机场配额 / quality 过滤 /
    家宽原生优先 等。预留参数：--min-stability --region --max-delay
    """
    return pool


def ordered_proxy(p: dict) -> dict:
    out = {k: p[k] for k in ("name", "type", "server", "port") if k in p}
    for k, v in p.items():
        if k not in out:
            out[k] = v
    return out


def emit(st: dict, tiers: list[int], outdir: Path, args) -> dict:
    written = {}
    nodes = [n for n in st["nodes"].values()
             if n["present"] and not n["blocked"] and not provider_excluded(n, args)]
    for t in tiers:
        pool = [n for n in nodes if n["tier"] >= t]
        pool = apply_filters(pool, args)
        pool.sort(key=lambda n: n["name"])
        path = outdir / TIER_FILES[t]
        header = (f"# {TIER_FILES[t]}\n"
                  f"# 门槛: {TIER_DESC[t]}\n"
                  f"# generated: {now_str()} | nodes: {len(pool)}\n"
                  f"# 由 ai_clean.py 自动生成，请勿手改\n")
        body = yaml.safe_dump({"proxies": [ordered_proxy(n["proxy"]) for n in pool]},
                              allow_unicode=True, sort_keys=False, width=4096)
        if not pool and path.exists() and path.stat().st_size > 0:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        atomic_write(path, header + body)
        written[t] = (path, len(pool))
    return written


# ============================== 汇总展示 ==============================
def show_status(st: dict, args):
    nodes = [n for n in st["nodes"].values()
             if n["present"] and not provider_excluded(n, args)]
    if args.exclude_providers:
        console.print(f"[dim]报告已排除机场: {','.join(args.exclude_providers)}[/dim]")
    if not nodes:
        console.print("[yellow]state 中没有可展示的在场节点(可能全部被排除)[/yellow]")
        return
    t = Table(title=f"节点总览 (state 更新于 {st.get('updated_at')})", box=box.SIMPLE_HEAVY)
    for c in ("机场", "在场", "T1", "T2", "T3", "待定", "封盘", "均延迟"):
        t.add_column(c, justify="right" if c != "机场" else "left")
    for prov in sorted({n["provider"] for n in nodes}):
        ns = [n for n in nodes if n["provider"] == prov]
        c = lambda k: sum(1 for n in ns if n["tier"] == k)
        pend = sum(1 for n in ns if unlock_pending(n))
        seal = sum(1 for n in ns if n["blocked"])
        ds = [n["reach"]["delay_ewma"] for n in ns if n["reach"]["delay_ewma"]]
        t.add_row(prov, str(len(ns)), str(c(1)), str(c(2)), str(c(3)), str(pend), str(seal),
                  f"{sum(ds)//len(ds)}ms" if ds else "-")
    console.print(t)

    sv = Table(title="服务解锁矩阵 (1=解锁 / 0=不解锁 / ·=未定论)", box=box.SIMPLE)
    sv.add_column("服务")
    for c in ("✅1", "🚫0", "·null", "止损"):
        sv.add_column(c, justify="right")
    for s in SERVICES:
        vs = [n["services"][s] for n in nodes]
        sv.add_row(s, str(sum(1 for x in vs if x["v"] == 1)),
                   str(sum(1 for x in vs if x["v"] == 0)),
                   str(sum(1 for x in vs if x["v"] is None)),
                   str(sum(1 for x in vs if x["quality"] == "gave_up")))
    console.print(sv)
    zr = Table(title="0 值原因分布 (quality)", box=box.SIMPLE)
    zr.add_column("服务")
    zr.add_column("原因")
    zr.add_column("数量", justify="right")
    for s in SERVICES:
        cnt: dict[str, int] = {}
        for n in nodes:
            x = n["services"][s]
            if x["v"] == 0:
                k = x["quality"] or "strong"
                cnt[k] = cnt.get(k, 0) + 1
        for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]):
            zr.add_row(s, k, str(v))
    if zr.row_count:
        console.print(zr)

    alive = [n for n in nodes if not n["blocked"] and n["tier"] >= 1]
    if alive:
        alive.sort(key=lambda n: (n["reach"]["delay_ewma"] or 99999))
        top = Table(title="Top 15 可用节点 (按 EWMA 延迟)", box=box.SIMPLE)
        for c in ("节点", "档", "地区", "延迟", "稳定(总/48h)", "链路"):
            top.add_column(c)
        for n in alive[:15]:
            sg, sr, sl = stability(n), stability_recent(n), stability(n, "link")
            f = lambda x: f"{x*100:.0f}%" if x is not None else "-"
            #top.add_row(n["name"][:52], f"T{n['tier']}", n["region"].get("apple", "??"),
            reg = (n["region"].get("apple") or n["region"].get("youtube")
                   or n["region"].get("tiktok") or "??")
            top.add_row(n["name"][:52], f"T{n['tier']}", reg,
                        f"{n['reach']['delay_ewma']}ms", f"{f(sg)}/{f(sr)}", f(sl))

        console.print(top)

    blocked = [n for n in nodes if n["blocked"]]
    if blocked:
        reasons: dict[str, int] = {}
        for n in blocked:
            k = (n["blocked_reason"] or "?").split(":")[0]
            reasons[k] = reasons.get(k, 0) + 1
        console.print("[dim]封盘原因: " + ", ".join(f"{k}×{v}" for k, v in
                                                  sorted(reasons.items(), key=lambda x: -x[1])) + "[/dim]")

def stability_rows(st: dict, args) -> list[dict]:
    rows = []
    for n in st["nodes"].values():
        if not n.get("present") or provider_excluded(n, args):
            continue
        r = n["reach"]
        g, rec, link = stability(n), stability_recent(n), stability(n, "link")
        ip_info = n.get("ip") or {}                     # [IPQS]
        iq = n.get("ipqs") or {}
        ia = n.get("ipapi") or {}
        ab = n.get("abuseipdb") or {}
        pu = compute_purity(n)
        iq_flags = ",".join(k for k in ("proxy", "vpn", "active_vpn", "tor", "active_tor", "recent_abuse", "bot_status") if iq.get(k))
        ia_flags = ",".join(k for k in ("is_abuser", "is_vpn", "is_proxy", "is_tor") if ia.get(k))
        rows.append({
            "provider": n["provider"], "name": n["name"],
            "region": n["region"].get("apple") or "",
            "region_yt": n["region"].get("youtube") or "",  # [IPQS]
            "tier": n["tier"], "blocked": int(n["blocked"]),
            "samples": r["samples"], "google_ok": r["google_ok"],
            "google_rate": round(g, 4) if g is not None else "",
            "recent_48h": round(rec, 4) if rec is not None else "",
            "link_rate": round(link, 4) if link is not None else "",
            "delay_last_ms": r["delay_last"] if r["delay_last"] is not None else "",
            "delay_ewma_ms": r["delay_ewma"] if r["delay_ewma"] is not None else "",
            "consecutive_fail": r["consecutive_fail"],
            "last_google_ok": "" if n["last_google_ok"] is None else int(n["last_google_ok"]),
            "last_ok_at": r["last_ok_at"] or "", "last_seen": n["last_seen"],
            "exit_ip": ip_info.get("addr") or "",           # [IPQS]
            "exit_cf_loc": ip_info.get("cf_loc") or "",
            "exit_ip_changes": ip_info.get("changes", 0) if ip_info.get("addr") else "",
            "ipqs_score": iq.get("fraud_score") if iq.get("ok") and iq.get("fraud_score") is not None else "",
            "ipqs_flags": iq_flags if iq.get("ok") else "",
            "ipqs_isp": iq.get("ISP") or "" if iq.get("ok") else "",
            "ipqs_asn": iq.get("ASN") if iq.get("ok") and iq.get("ASN") is not None else "",
            "ipapi_dc": int(bool(ia.get("is_datacenter"))) if ia.get("ok") else "",
            "ipapi_type": ia.get("company_type") or "" if ia.get("ok") else "",
            "ipapi_asn_abuse": ia.get("asn_abuser_score") if ia.get("ok") and ia.get("asn_abuser_score") is not None else "",
            "ipapi_co_abuse": ia.get("company_abuser_score") if ia.get("ok") and ia.get("company_abuser_score") is not None else "",
            "ipapi_flags": ia_flags if ia.get("ok") else "",
            "abuse_score": ab.get("abuse_score") if ab.get("ok") and ab.get("abuse_score") is not None else "",
            "abuse_reports": ab.get("total_reports") if ab.get("ok") and ab.get("total_reports") is not None else "",
            "purity_grade": pu["grade"],
            "purity_notes": "; ".join(pu["flags"]),
        })
    rows.sort(key=lambda x: (x["provider"], x["name"]))
    return rows


def dump_stats(st: dict, path: Path, args):
    rows = stability_rows(st, args)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        import csv
        cols = list(rows[0].keys()) if rows else ["provider", "name"]
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    else:
        atomic_write(path, json.dumps({"generated": now_str(), "nodes": rows},
                                      ensure_ascii=False, indent=1))
    console.print(f"[green]统计已导出[/green] {len(rows)} 节点 → {path}")


def show_stability(st: dict, args):
    nodes = [n for n in st["nodes"].values()
             if n.get("present") and not provider_excluded(n, args)]
    if not nodes:
        console.print("[yellow]state 中没有可展示的在场节点(可能全部被排除)[/yellow]")
        return
    mean = lambda xs: sum(xs) / len(xs) if xs else None
    pct = lambda v: f"{v*100:.0f}%" if v is not None else "-"

    t = Table(title="稳定性/延迟 · 按机场汇总", box=box.SIMPLE_HEAVY)
    for c in ("机场", "节点", "本轮存活", "均Google成功率", "均48h", "均链路", "均EWMA延迟"):
        t.add_column(c, justify="left" if c == "机场" else "right")
    for prov in sorted({n["provider"] for n in nodes}):
        ns = [n for n in nodes if n["provider"] == prov]
        g = mean([x for x in (stability(n) for n in ns) if x is not None])
        rc = mean([x for x in (stability_recent(n) for n in ns) if x is not None])
        lk = mean([x for x in (stability(n, "link") for n in ns) if x is not None])
        ds = [n["reach"]["delay_ewma"] for n in ns if n["reach"]["delay_ewma"]]
        alive = sum(1 for n in ns if n.get("last_google_ok"))
        t.add_row(prov, str(len(ns)), str(alive), pct(g), pct(rc), pct(lk),
                  f"{sum(ds)//len(ds)}ms" if ds else "-")
    console.print(t)

    top = Table(title="Top 20 稳定节点 (48h成功率↓, EWMA延迟↑)", box=box.SIMPLE)
    for c in ("节点", "机场", "地区", "Google(总/48h)", "链路", "延迟last/EWMA", "连败", "最近成功"):
        top.add_column(c)
    ranked = [n for n in nodes if n["reach"]["samples"]]
    ranked.sort(key=lambda n: (-(stability_recent(n) or stability(n) or 0),
                               n["reach"]["delay_ewma"] or 99999))
    for n in ranked[:20]:
        r = n["reach"]
        top.add_row(n["name"][:48], n["provider"], n["region"].get("apple", "??"),
                    f"{pct(stability(n))}/{pct(stability_recent(n))}",
                    pct(stability(n, "link")),
                    f"{r['delay_last'] or '-'}/{r['delay_ewma'] or '-'}ms",
                    str(r["consecutive_fail"]), r["last_ok_at"] or "-")
    console.print(top)

# ============================== 定时任务向导 ==============================
def _ps(script: str) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as f:
        f.write(script)
        p = f.name
    try:
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", p],
                              capture_output=True, text=True, timeout=120,
                              creationflags=CREATE_NO_WINDOW)
    finally:
        try:
            os.unlink(p)
        except Exception:
            pass


def append_provider_args(argv: list[str], args) -> list[str]:
    """把 --provider / --exclude-provider 原样带入定时任务命令行。"""
    if args.provider:
        argv += ["--provider", args.provider]
    for name in (getattr(args, "exclude_provider", None) or []):
        argv += ["--exclude-provider", name]
    return argv


def _psq(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


#  def install_task(args):
def install_task_windows(args):
    if os.name != "nt":
        console.print("[red]定时任务向导仅支持 Windows[/red]")
        return 1
    console.print(Panel.fit("[bold]AI 采样定时任务 · 安装向导[/bold]\n"
                            "账户模式：仅当前用户登录时运行（个人电脑推荐，无需密码/管理员）",
                            border_style="cyan"))
    subs = args.subs or Path(Prompt.ask("订阅清单路径", default=str(Path("subs.txt").resolve())))
    subs = Path(subs).resolve()
    if not subs.is_file():
        console.print(f"[red]订阅清单不存在: {subs}[/red]")
        return 1
    script = Path(__file__).resolve()
    state = Path(args.state).resolve()
    outdir = Path(args.out_dir).resolve()
    name = Prompt.ask("任务名称", default="AI-Clean-Sample")
    minutes = IntPrompt.ask("心跳间隔(分钟)", default=30)
    tiers = Prompt.ask("输出档位(t1,t2,t3)", default=args.out_tiers)
    conc = min(MAX_CONCURRENCY, IntPrompt.ask(f"并发(上限{MAX_CONCURRENCY})", default=args.concurrency))
    limit = IntPrompt.ask("单次运行时长上限(分钟)", default=30)
    wake = Confirm.ask("唤醒计算机执行", default=False)
    highest = Confirm.ask("以最高权限运行(需管理员，一般不需要)", default=False)

    argv = [str(script), "--subs", str(subs), "--state", str(state),
            "--out-dir", str(outdir), "--out-tiers", tiers, "-c", str(conc)]
    if args.mihomo:
        argv += ["--mihomo", str(args.mihomo)]
    append_provider_args(argv, args)
    #  if args.no_stability_sample:
        #  argv += ["--no-stability-sample"]
    #  if args.stability_only:
        #  argv += ["--stability-only"]
    #  inner = " ".join(a if a.startswith("-") else f'"{a}"' for a in argv)
#  
    if args.no_stability_sample:
        argv += ["--no-stability-sample"]
    if args.stability_only:
        argv += ["--stability-only"]
    if args.stats_out:
        argv += ["--stats-out", str(Path(args.stats_out).resolve())]
    inner = " ".join(a if a.startswith("-") else f'"{a}"' for a in argv)

    settings = ["-StartWhenAvailable", "-MultipleInstances IgnoreNew",
                "-AllowStartIfOnBatteries", "-DontStopIfGoingOnBatteries",
                f"-ExecutionTimeLimit (New-TimeSpan -Minutes {limit})"]
    if wake:
        settings.append("-WakeToRun")
    user = f"{os.environ.get('USERDOMAIN','')}\\{os.environ.get('USERNAME','')}"
    ps = f"""$ErrorActionPreference='Stop'
$act = New-ScheduledTaskAction -Execute {_psq(sys.executable)} -Argument {_psq(inner)} -WorkingDirectory {_psq(str(script.parent))}
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes {minutes}) -RepetitionDuration ([TimeSpan]::MaxValue)
$set = New-ScheduledTaskSettings {' '.join(settings)}
$pri = New-ScheduledTaskPrincipal -UserId {_psq(user)} -LogonType Interactive{' -RunLevel Highest' if highest else ''}
Register-ScheduledTask -TaskName {_psq(name)} -Action $act -Trigger $trg -Settings $set -Principal $pri -Description 'AI clean node sampler (ai_clean.py)' -Force | Out-Null
Write-Output 'OK'
"""
    console.print(f"[dim]将执行: {sys.executable} {inner}[/dim]")
    if not Confirm.ask("确认创建/覆盖该任务?", default=True):
        return 0
    r = _ps(ps)
    if r.returncode != 0 or "OK" not in (r.stdout or ""):
        console.print(f"[red]创建失败:[/red]\n{(r.stderr or r.stdout or '').strip()[:800]}")
        return 1
    console.print(f"[green]✅ 已创建任务 {name}，每 {minutes} 分钟心跳一次[/green]\n"
                  f"   查看状态: python {script.name} --task-status\n"
                  f"   卸载:     python {script.name} --uninstall-task")
    return 0


#  def uninstall_task(args):
def uninstall_task_windows(args):
    r = _ps("Get-ScheduledTask -TaskName 'AI-*' | Select-Object -ExpandProperty TaskName")
    names = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    if not names:
        console.print("[yellow]没有找到 AI-* 定时任务[/yellow]")
        return 0
    console.print("找到任务: " + ", ".join(f"[{i+1}]{n}" for i, n in enumerate(names)))
    pick = Prompt.ask("要删除哪个(序号, 0=取消)", default="0")
    if not pick.isdigit() or int(pick) == 0 or int(pick) > len(names):
        return 0
    name = names[int(pick) - 1]
    if not Confirm.ask(f"确认删除 {name}?", default=False):
        return 0
    r = _ps(f"Unregister-ScheduledTask -TaskName {_psq(name)} -Confirm:$false; Write-Output 'OK'")
    console.print("[green]已删除[/green]" if "OK" in (r.stdout or "") else f"[red]{r.stderr[:400]}[/red]")
    return 0


#  def task_status(args):
def task_status_windows(args):
    ps = """$ErrorActionPreference='SilentlyContinue'
$t = Get-ScheduledTask -TaskName 'AI-*'
if (-not $t) { Write-Output '[]'; exit }
$out = @()
foreach ($x in $t) {
  $i = Get-ScheduledTaskInfo -TaskName $x.TaskName
  $out += [pscustomobject]@{Name=$x.TaskName; State=$x.State; Last=$i.LastRunTime; Result=$i.LastTaskResult; Next=$i.NextRunTime; Missed=$i.NumberOfMissedRuns}
}
$out | ConvertTo-Json -Compress
"""
    r = _ps(ps)
    try:
        data = json.loads((r.stdout or "[]").strip() or "[]")
    except Exception:
        console.print(f"[red]查询失败: {(r.stderr or '')[:300]}[/red]")
        return 1
    if isinstance(data, dict):
        data = [data]
    if not data:
        console.print("[yellow]没有 AI-* 定时任务[/yellow]")
        return 0
    t = Table(title="定时任务状态", box=box.SIMPLE_HEAVY)
    for c in ("名称", "状态", "上次运行", "退出码", "下次运行", "错过"):
        t.add_column(c)
    for d in data:
        t.add_row(str(d.get("Name")), str(d.get("State")), str(d.get("Last")),
                  str(d.get("Result")), str(d.get("Next")), str(d.get("Missed")))
    console.print(t)
    return 0

UNIT_PREFIX = "ai-clean-"

def install_task(args):
    return install_task_windows(args) if os.name == "nt" else install_task_linux(args)

def uninstall_task(args):
    return uninstall_task_windows(args) if os.name == "nt" else uninstall_task_linux(args)

def task_status(args):
    return task_status_windows(args) if os.name == "nt" else task_status_linux(args)


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, **kw)

def _systemd_user_ok() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        return _run(["systemctl", "--user", "show-environment"]).returncode == 0
    except Exception:
        return False

def _user_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"

def _sd_show(unit: str, *props: str) -> dict:
    cmd = ["systemctl", "--user", "show", unit]
    for p in props:
        cmd += ["-p", p]
    r = _run(cmd)
    return dict(l.split("=", 1) for l in (r.stdout or "").splitlines() if "=" in l)

def _cron_lines() -> list[str]:
    try:
        r = _run(["crontab", "-l"])
    except Exception:
        return []
    return (r.stdout or "").splitlines() if r.returncode == 0 else []

def _cron_write(lines: list[str]):
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                   text=True, check=True, capture_output=True)


def install_task_linux(args):
    console.print(Panel.fit("[bold]AI 采样定时任务 · 安装向导 (Linux)[/bold]\n"
                            "优先 systemd 用户级 timer(无需 root)；无用户会话时回退 cron",
                            border_style="cyan"))
    subs = args.subs or Path(Prompt.ask("订阅清单路径", default=str(Path("subs.txt").resolve())))
    subs = Path(subs).resolve()
    if not subs.is_file():
        console.print(f"[red]订阅清单不存在: {subs}[/red]")
        return 1
    script = Path(__file__).resolve()
    state, outdir = Path(args.state).resolve(), Path(args.out_dir).resolve()
    name = re.sub(r"[^A-Za-z0-9_-]", "-", Prompt.ask("任务名称", default="ai-clean-sample"))
    if not name.startswith(UNIT_PREFIX):
        name = UNIT_PREFIX + name
    minutes = IntPrompt.ask("心跳间隔(分钟)", default=30)
    tiers = Prompt.ask("输出档位(t1,t2,t3)", default=args.out_tiers)
    conc = min(MAX_CONCURRENCY, IntPrompt.ask(f"并发(上限{MAX_CONCURRENCY})", default=args.concurrency))
    limit = IntPrompt.ask("单次运行时长上限(分钟)", default=30)
    stab = Confirm.ask("只测稳定性/延迟(--stability-only)?", default=args.stability_only)
    refresh = Confirm.ask("每轮都刷新订阅?(一般选否，用缓存节点池)", default=False)

    argv = [str(script), "--subs", str(subs), "--state", str(state),
            "--out-dir", str(outdir), "--out-tiers", tiers, "-c", str(conc)]
    if args.mihomo:
        argv += ["--mihomo", str(args.mihomo)]
    append_provider_args(argv, args)
    if refresh:
        argv += ["--refresh-subs"]

    if stab:
        argv += ["--stability-only"]
    elif args.no_stability_sample:
        argv += ["--no-stability-sample"]
    if args.stats_out:
        argv += ["--stats-out", str(Path(args.stats_out).resolve())]
    exec_line = " ".join(shlex.quote(a) for a in [sys.executable] + argv)

    if _systemd_user_ok():
        linger = Confirm.ask("未登录/注销后也要运行? (loginctl enable-linger)", default=False)
        console.print(f"[dim]将执行: {exec_line}[/dim]")
        if not Confirm.ask(f"确认创建/覆盖 {name}.timer?", default=True):
            return 0
        udir = _user_unit_dir()
        udir.mkdir(parents=True, exist_ok=True)
        (udir / f"{name}.service").write_text(
            f"[Unit]\nDescription=AI clean node sampler (ai_clean.py)\n\n"
            f"[Service]\nType=oneshot\nWorkingDirectory={script.parent}\n"
            f"ExecStart={exec_line}\nTimeoutStartSec={limit * 60}\nNice=10\n",
            encoding="utf-8")
        (udir / f"{name}.timer").write_text(
            f"[Unit]\nDescription=AI clean sampler timer (every {minutes}min)\n\n"
            f"[Timer]\nOnActiveSec=1min\nOnUnitActiveSec={minutes}min\n"
            f"AccuracySec=30s\nPersistent=true\n\n"
            f"[Install]\nWantedBy=timers.target\n", encoding="utf-8")
        r1 = _run(["systemctl", "--user", "daemon-reload"])
        r2 = _run(["systemctl", "--user", "enable", "--now", f"{name}.timer"])
        if r1.returncode != 0 or r2.returncode != 0:
            console.print(f"[red]创建失败:[/red]\n{(r2.stderr or r1.stderr).strip()[:800]}")
            return 1
        if linger:
            if _run(["loginctl", "enable-linger", getpass.getuser()]).returncode != 0:
                console.print("[yellow]enable-linger 失败，可手动: "
                              f"sudo loginctl enable-linger {getpass.getuser()}[/yellow]")
        console.print(f"[green]✅ 已创建 {name}.timer，每 {minutes} 分钟一次[/green]\n"
                      f"   状态: python3 {script.name} --task-status\n"
                      f"   日志: journalctl --user -u {name}.service -f\n"
                      f"   卸载: python3 {script.name} --uninstall-task")
        return 0

    # ---- cron 回退 ----
    console.print("[yellow]未检测到可用的 systemd 用户会话，回退 cron[/yellow]")
    if not shutil.which("crontab"):
        console.print("[red]系统没有 crontab，请先: sudo apt install cron[/red]")
        return 1
    if not (1 <= minutes <= 59):
        console.print("[red]cron 模式间隔需在 1..59 分钟[/red]")
        return 1
    logfile = state.parent / "logs" / f"cron-{name}.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    marker = f"# ai-clean:{name}"
    cmd = (f"cd {shlex.quote(str(script.parent))} && {exec_line} "
           f">> {shlex.quote(str(logfile))} 2>&1").replace("%", r"\%")  # cron里%是特殊字符
    line = f"*/{minutes} * * * * {cmd} {marker}"
    console.print(f"[dim]将写入 crontab: {line}[/dim]")
    if not Confirm.ask("确认创建/覆盖该 cron 任务?", default=True):
        return 0
    try:
        _cron_write([l for l in _cron_lines() if marker not in l] + [line])
    except subprocess.CalledProcessError as e:
        console.print(f"[red]写入 crontab 失败: {(e.stderr or '')[:400]}[/red]")
        return 1
    console.print(f"[green]✅ 已写入 cron，每 {minutes} 分钟一次[/green]\n"
                  f"   日志: {logfile}\n   卸载: python3 {script.name} --uninstall-task")
    return 0


def uninstall_task_linux(args):
    items = []                                   # (kind, name)
    udir = _user_unit_dir()
    if udir.is_dir():
        items += [("systemd", t.stem) for t in sorted(udir.glob(f"{UNIT_PREFIX}*.timer"))]
    for l in _cron_lines():
        m = re.search(r"# ai-clean:(\S+)", l)
        if m:
            items.append(("cron", m.group(1)))
    if not items:
        console.print("[yellow]没有找到 ai-clean 定时任务[/yellow]")
        return 0
    console.print("找到任务: " + ", ".join(f"[{i+1}]{k}:{n}" for i, (k, n) in enumerate(items)))
    pick = Prompt.ask("要删除哪个(序号, 0=取消)", default="0")
    if not pick.isdigit() or int(pick) == 0 or int(pick) > len(items):
        return 0
    kind, name = items[int(pick) - 1]
    if not Confirm.ask(f"确认删除 {kind}:{name}?", default=False):
        return 0
    if kind == "systemd":
        _run(["systemctl", "--user", "disable", "--now", f"{name}.timer"])
        _run(["systemctl", "--user", "stop", f"{name}.service"])
        for ext in (".timer", ".service"):
            (udir / f"{name}{ext}").unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    else:
        _cron_write([l for l in _cron_lines() if f"# ai-clean:{name}" not in l])
    console.print("[green]已删除[/green]")
    return 0


def task_status_linux(args):
    rows = []
    udir = _user_unit_dir()
    if udir.is_dir() and _systemd_user_ok():
        for tmr in sorted(udir.glob(f"{UNIT_PREFIX}*.timer")):
            name = tmr.stem
            tp = _sd_show(f"{name}.timer", "ActiveState", "LastTriggerUSec",
                          "NextElapseUSecRealtime", "NextElapseUSecMonotonic")
            sp = _sd_show(f"{name}.service", "ExecMainStartTimestamp", "ExecMainStatus")
            nxt = tp.get("NextElapseUSecRealtime", "")
            if nxt in ("", "n/a", "0"):
                nxt = tp.get("NextElapseUSecMonotonic", "-")
            rows.append((name, tp.get("ActiveState", "?"),
                         sp.get("ExecMainStartTimestamp") or tp.get("LastTriggerUSec") or "-",
                         sp.get("ExecMainStatus", "-"), nxt))
    for l in _cron_lines():
        m = re.search(r"# ai-clean:(\S+)", l)
        if m:
            rows.append((m.group(1) + " (cron)", "scheduled", "-", "-", l.split()[0]))
    if not rows:
        console.print("[yellow]没有 ai-clean 定时任务[/yellow]")
        return 0
    t = Table(title="定时任务状态 (Linux)", box=box.SIMPLE_HEAVY)
    for c in ("名称", "状态", "上次运行", "退出码", "下次运行"):
        t.add_column(c)
    clean = lambda v: "-" if str(v) in ("", "n/a") else str(v)
    for r in rows:
        t.add_row(*(clean(x) for x in r))
    console.print(t)
    return 0

# ============================== 锁 ==============================
class RunLock:
    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            age = time.time() - self.path.stat().st_mtime
            if age < 6 * 3600:
                raise RuntimeError(f"已有一轮在运行(锁 {self.path.name}, {age/60:.0f}分钟前)，本次退出")
            log("发现过期锁，强制接管")
            self.path.unlink(missing_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {now_str()}".encode())
        os.close(fd)
        self.acquired = True
        return self

    def __exit__(self, *a):
        if self.acquired:
            try:
                self.path.unlink(missing_ok=True)
            except Exception:
                pass


# ============================== 主流程 ==============================

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ai_clean.py",
        description="AI 干净节点筛选器 v3 —— 解锁一次性增量探测 + 周期性健康采样 + IP 纯净度(IPQS/ipapi.is/AbuseIPDB)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""推荐工作流示例:
  1) 一次性: 从现有 Clash 配置提取订阅清单
     python ai_clean.py --from-clash myclash.yaml --dump-subs subs.txt

  2) 刷新节点池(拉订阅, 顺带一轮稳定性采样, 不动解锁结论)
     python ai_clean.py --subs subs.txt --refresh-subs --stability-only

  3) 后台定时稳定性/延迟采样(向导创建计划任务, 每30分钟, 导出CSV)
     python ai_clean.py --install-task --subs subs.txt --stability-only --stats-out stats.csv
     python ai_clean.py --task-status
     python ai_clean.py --uninstall-task

  4) 手动跑一两次解锁探测(只补 state 里还是 null 的项, 已封盘永不重测)
     python ai_clean.py --subs subs.txt --out-tiers t1,t2,t3 --no-stability-sample
     python ai_clean.py --subs subs.txt --out-tiers t1,t2,t3 --provider 某机场

  5) IP 纯净度(取出口IP → 直连查 IPQS + 可选 ipapi.is / AbuseIPDB; key 可放环境变量)
     python ai_clean.py --subs subs.txt --ipqs-only --ipqs-min-tier 2 --stats-out ipqs.csv
     python ai_clean.py --subs subs.txt --ipqs --no-stability-sample --ipqs-min-tier 2
     python ai_clean.py --subs subs.txt --ipqs-only --ipqs-refresh --ipqs-block-above 90

  6) 离线查看 / 输出
     python ai_clean.py --status
     python ai_clean.py --status --stability-only --stats-out stats.csv
     python ai_clean.py --emit-only --out-tiers t1,t2,t3

其他常用:
  python ai_clean.py --subs subs.txt --refresh-subs --sub-cookie "cf_clearance=..."
  python ai_clean.py --emit-only --out-tiers t1,t2,t3 --exclude-provider 某机场
  python ai_clean.py --subs subs.txt --force-unlock --provider 某机场
  python ai_clean.py --subs subs.txt --reset-stats

说明:
  * 不加 --refresh-subs 就绝不拉订阅(直接用 state 缓存节点池), 后台任务建议保持不加, 避免机场风控。
  * T1=ChatGPT+YouTube Premium; T2=T1+Claude+TikTok; T3=T2+Netflix(完整)+Disney++Prime Video。
  * 解锁结论永久封盘; --stability-only / --ipqs-only 不改解锁/tier(--ipqs-block-above 除外)。
  * 并发硬上限 3; --batch-size 控制单批节点数(每批一个 mihomo 实例, 端口池钉死节点)。
""")
    g = ap.add_argument_group("订阅与节点池")
    g.add_argument("--subs", type=Path, metavar="FILE", help="订阅清单, 每行: 机场名|URL (# 开头为注释)")
    g.add_argument("--from-clash", type=Path, metavar="FILE", help="从 Clash 配置提取 proxy-providers 的订阅 URL")
    g.add_argument("--dump-subs", type=Path, metavar="FILE", help="把提取到的订阅写入清单后退出(配合 --from-clash)")
    g.add_argument("--refresh-subs", action="store_true", help="真正拉取订阅并增量并入节点池(默认不拉, 用 state 缓存)")
    g.add_argument("--refresh-only", action="store_true",
                   help="只拉订阅刷新节点池后退出: 不起内核/不测解锁/不采样/不生成yaml")
    g.add_argument("--ua", default=UA_SUB, metavar="UA",
                   help=f"订阅 UA(默认 {UA_SUB.strip()}; 保持 clash 系, 机场按 UA 决定返回格式)")
    g.add_argument("--sub-engine", choices=["auto", "cffi", "requests"], default="auto",
                   help="订阅引擎: cffi=curl_cffi(Chrome TLS 指纹, 需 pip install curl_cffi); auto=装了就用(默认)")
    g.add_argument("--sub-cookie", default=None, metavar="STR", help="订阅请求附加 Cookie(浏览器 F12 复制的 WAF cookie)")
    g.add_argument("--sub-interval", type=float, default=2.0, metavar="SEC",
                   help="相邻订阅拉取的基础间隔秒(另加随机抖动, 默认 2)")
    g.add_argument("--exclude-keywords", default=None, help="覆盖默认信息节点过滤词(逗号分隔)")
    g.add_argument("--extra-exclude", default=None, help="追加过滤词(逗号分隔)")
    g.add_argument("--no-name-filter", action="store_true", help="关闭节点名关键词过滤")
    g.add_argument("--provider", metavar="NAME", help="只处理该机场的节点")
    g.add_argument("--exclude-provider", action="append", default=[], metavar="NAME",
                   help="输出与报告中排除的机场(可多次或逗号分隔)")
    g.add_argument("--limit", type=int, default=0, metavar="N",
                   help="本轮最多处理 N 个节点(0=不限; 控制 IPQS 配额时很有用)")

    g = ap.add_argument_group("探测与采样")
    g.add_argument("--mihomo", metavar="PATH", help="mihomo/verge-mihomo 路径(默认自动查找 Clash Verge)")
    g.add_argument("-c", "--concurrency", type=int, default=MAX_CONCURRENCY,
                   help=f"并发线程数(硬上限 {MAX_CONCURRENCY}, 防机场限流)")
    g.add_argument("--timeout", type=float, default=8, help="单次 HTTP 读超时秒(默认 8)")
    g.add_argument("--retries", type=int, default=1, help="单项探测网络错误重试次数(默认 1)")
    g.add_argument("--batch-size", type=int, default=400, help="每批节点数(默认 400; 内核拒绝时自动二分排障)")
    g.add_argument("--max-attempts", type=int, default=5, help="单项服务累计未定论止损次数(默认 5, 跨轮累计)")
    g.add_argument("--blocked-regions", default=", ".join(DEFAULT_BLOCKED_REGIONS),
                   help=f"Apple GCC 命中即封盘的地区(默认 {','.join(DEFAULT_BLOCKED_REGIONS)})")
    g.add_argument("--force-unlock", action="store_true", help="清空解锁结论/封盘状态, 强制重测(慎用)")
    g.add_argument("--no-stability-sample", action="store_true", help="本轮跳过健康采样(只测解锁)")
    g.add_argument("--stability-only", action="store_true",
                   help="只测稳定性/延迟(204+失败时 Apple 兜底), 跳过解锁, 不改档不封盘")
    g.add_argument("--reset-stats", action="store_true", help="清零健康统计(保留解锁结论), 开启全新统计窗口")

    g = ap.add_argument_group("IP 纯净度 (IPQS + ipapi.is + AbuseIPDB)")
    g.add_argument("--ipqs", action="store_true", help="本轮顺带做 IP 纯净度查询(已查成功的跳过)")
    g.add_argument("--ipqs-only", action="store_true", help="只做 IP 纯净度(取出口IP+直连评分), 跳过解锁与健康采样")
    g.add_argument("--ipqs-key", default=None, metavar="KEY", help="IPQS API key(也可用环境变量 IPQS_KEY)")
    g.add_argument("--ipapi-key", default=None, metavar="KEY",
                   help="ipapi.is API key(可选, 环境变量 IPAPI_KEY; 查 ASN/机房滥用率)")
    g.add_argument("--abuseipdb-key", default=None, metavar="KEY",
                   help="AbuseIPDB API key(可选, 环境变量 ABUSEIPDB_KEY; 查近期攻击举报)")
    g.add_argument("--ipqs-min-tier", type=int, default=0, choices=[0, 1, 2, 3], metavar="0-3",
                   help="只对 tier>=N 的节点查 IP 纯净度(默认 0=全部未封盘; 可设 2/3 专筛 T2/T3)")
    g.add_argument("--ipqs-refresh", action="store_true", help="强制重新取出口IP并重查(默认复用已成功记录)")
    g.add_argument("--ipqs-strictness", type=int, default=1, metavar="0-3",
                   help="IPQS 严格度(默认 1; 官方建议 0/1, 2+ 很严格易误报)")
    g.add_argument("--ipqs-interval", type=float, default=1.5, metavar="SEC",
                   help="IPQS 相邻查询最小间隔秒(默认 1.5, 保护配额)")
    g.add_argument("--ipqs-block-above", type=int, default=None, metavar="N",
                   help="fraud_score>=N 或 Tor 即封盘(默认不封, 只记录)")

    g = ap.add_argument_group("输出与状态")
    g.add_argument("--state", type=Path, default=Path("ai_state.json"), help="状态文件(默认 ai_state.json)")
    g.add_argument("--out-dir", type=Path, default=Path("."), help="yaml 输出目录(默认当前目录)")
    g.add_argument("--out-tiers", default="t1", metavar="LIST", help="输出档位 t1,t2,t3(默认 t1)")
    g.add_argument("--stats-out", type=Path, metavar="FILE",
                   help="导出每节点稳定性/延迟/IPQS 统计(.csv 或 .json)")
    g.add_argument("--emit-only", action="store_true", help="不联网, 只从 state 重新生成 yaml")
    g.add_argument("--status", action="store_true", help="不联网, 只看汇总(加 --stability-only 看稳定性表)")
    g.add_argument("--keep-temp", action="store_true", help="保留临时目录/内核日志(排障用)")
    g.add_argument("-v", "--verbose", action="store_true", help="详细日志")

    g = ap.add_argument_group("定时任务")
    g.add_argument("--install-task", action="store_true",
                   help="安装定时任务向导(Windows 计划任务 / Linux systemd+cron)")
    g.add_argument("--uninstall-task", action="store_true", help="卸载定时任务(交互选择)")
    g.add_argument("--task-status", action="store_true", help="查看定时任务状态")

    g = ap.add_argument_group("预留(筛选算法钩子, 当前未启用)")
    g.add_argument("--min-stability", type=float, default=None, help="[预留未启用]")
    g.add_argument("--region", default=None, help="[预留未启用]")
    g.add_argument("--max-delay", type=int, default=None, help="[预留未启用]")
    return ap

def main() -> int:
    global _LOG_FH
    ap = build_parser()
    args = ap.parse_args()

    if args.stability_only and args.no_stability_sample:
        console.print("[red]--stability-only 与 --no-stability-sample 互斥[/red]")
        return 1
    # [IPQS] 规范化与互斥校验
    if args.ipqs_only:
        args.ipqs = True
    if args.ipqs_only and args.stability_only:
        console.print("[red]--ipqs-only 与 --stability-only 互斥[/red]")
        return 1
    args.ipqs_key = (args.ipqs_key or os.environ.get("IPQS_KEY") or "").strip() or None
    args.ipapi_key = (args.ipapi_key or os.environ.get("IPAPI_KEY") or "").strip() or None
    args.abuseipdb_key = (args.abuseipdb_key or os.environ.get("ABUSEIPDB_KEY") or "").strip() or None
    if not (0 <= args.ipqs_strictness <= 3):
        console.print("[red]--ipqs-strictness 必须在 0..3 之间[/red]")
        return 1
    args.ipqs_interval = max(0.0, float(args.ipqs_interval))

    if args.refresh_only:
        args.refresh_subs = True          # --refresh-only 隐含 --refresh-subs
    if args.concurrency > MAX_CONCURRENCY:
        console.print(f"[yellow]并发 {args.concurrency} 超过上限，已降为 {MAX_CONCURRENCY}(防机场限流)[/yellow]")
        args.concurrency = MAX_CONCURRENCY
    args.concurrency = max(1, args.concurrency)
    args.blocked_regions = [x.strip().upper() for x in split_kw(args.blocked_regions)]
    args.exclude_providers = [p for v in args.exclude_provider for p in split_kw(v)]
    try:
        tiers = parse_tiers(args.out_tiers)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return 1

    if args.install_task:
        return install_task(args)
    if args.uninstall_task:
        return uninstall_task(args)
    if args.task_status:
        return task_status(args)

    args.state = args.state.resolve()
    args.out_dir = args.out_dir.resolve()
    logdir = args.state.parent / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    _LOG_FH = open(logdir / f"run-{time.strftime('%Y%m%d')}.log", "a", encoding="utf-8")
    log("==== run start ====")

    st = load_state(args.state)

    #  if args.status:
        #  show_status(st, args)
        #  return 0

    if args.status:
        show_status(st, args)
        if args.stability_only:
            show_stability(st, args)
        if args.ipqs or any_ip_purity_data(st):          # [IPQS]
            show_ipqs(st, args)
        if args.stats_out:
            dump_stats(st, args.stats_out, args)
        return 0

    if args.emit_only:
        w = emit(st, tiers, args.out_dir, args)
        for t, (p, cnt) in w.items():
            console.print(f"[green]T{t}[/green] {cnt:4d} 节点 → {p}")
        return 0

    # ---- 订阅来源 ----
    subs: list[Sub] = []
    if args.from_clash:
        subs += extract_from_clash(args.from_clash)
    if args.dump_subs:
        if not subs:
            console.print("[red]--dump-subs 需配合 --from-clash[/red]")
            return 1
        
        atomic_write(args.dump_subs, "# 机场名|订阅URL\n" +
                     "\n".join(f"{s.name}|{s.url}" for s in subs) + "\n")
        console.print(f"[green]已生成 {args.dump_subs} ({len(subs)} 条)，检查后用 --subs 运行[/green]")
        return 0
    if args.subs:
        subs += load_subs_file(args.subs)

    #  if not subs:
        #  console.print("[red]没有订阅来源，请用 --subs 或 --from-clash[/red]")
        #  return 1

    if args.refresh_subs and not subs:
        console.print("[red]--refresh-subs 需要订阅来源(--subs 或 --from-clash)[/red]")
        return 1
    if subs and not args.refresh_subs:
        console.print("[dim]未加 --refresh-subs：本轮不拉订阅，直接用 state 缓存节点池[/dim]")

    uniq, seen_u = [], set()
    for s in subs:
        if s.url not in seen_u:
            seen_u.add(s.url)
            uniq.append(s)
    subs = uniq

    if args.no_name_filter:
        keywords = []
    else:
        keywords = split_kw(args.exclude_keywords) if args.exclude_keywords else list(DEFAULT_EXCLUDE_KEYWORDS)
        if args.extra_exclude:
            keywords += split_kw(args.extra_exclude)

    if args.ipqs and not args.refresh_only and not args.ipqs_key:   # [IPQS]
        console.print("[red]启用 --ipqs/--ipqs-only 需要提供 --ipqs-key 或设置环境变量 IPQS_KEY[/red]")
        try:
            _LOG_FH.close()
        except Exception:
            pass
        return 1

    if args.refresh_only:
        exe, ver = "(未启动)", "只刷新订阅模式(不需要内核)"
    else:
        exe, ver = find_mihomo(args.mihomo)
    ip_engs = [name for name, ok in (("IPQS", args.ipqs_key), ("ipapi.is", args.ipapi_key),
                                     ("AbuseIPDB", args.abuseipdb_key)) if ok]
    ip_line = (f"\nIP纯净度: {' + '.join(ip_engs)} | min-tier=T{args.ipqs_min_tier}"
               f" | 间隔={args.ipqs_interval}s | 刷新={'是' if args.ipqs_refresh else '否'}") if args.ipqs else ""
    console.print(Panel.fit(
        f"[bold]AI 干净节点筛选器 v3[/bold]\n内核: {exe}\n版本: {ver}\n"
        f"T1 {TIER_DESC[1]}\nT2 {TIER_DESC[2]}\nT3 {TIER_DESC[3]}\n"
        #  f"并发 {args.concurrency}(上限3) | 批次 {args.batch_size} | 超时 {args.timeout}s | 订阅 {len(subs)}\n"
        f"并发 {args.concurrency}(上限3) | 批次 {args.batch_size} | 超时 {args.timeout}s | "
        f"节点池{'刷新订阅' if args.refresh_subs else '缓存'}({len(subs)}订阅)\n"
        f"名称过滤 {len(keywords)} 词 | 送中判定 {','.join(args.blocked_regions)} | 输出 {args.out_tiers}"
        + (f" | 排除机场(仅输出/报告) {','.join(args.exclude_providers)}" if args.exclude_providers else "")
        + ip_line,
        border_style="cyan"))

    try:
        with RunLock(args.state.with_suffix(".lock")):
            #  return run_round(st, subs, keywords, exe, ver, tiers, args)
            if args.refresh_only:
                new = refresh_subscriptions(st, subs, keywords, args)
                save_state(st, args.state)
                present = sum(1 for n in st["nodes"].values() if n["present"])
                console.print(f"[green]只刷新订阅完成[/green]：在场 {present} 个节点(新增 {new})，未起内核/未测解锁/未采样/未生成yaml")
                return 0
            return run_round(st, subs, keywords, exe, ver, tiers, args)
    except RuntimeError as e:
        console.print(f"[yellow]{e}[/yellow]")
        log(str(e))
        return 0
    except KeyboardInterrupt:
        console.print("[yellow]用户中断，已清理[/yellow]")
        save_state(st, args.state)
        return 130
    finally:
        _atexit_cleanup()
        log("==== run end ====")
        try:
            _LOG_FH.close()
        except Exception:
            pass


def refresh_subscriptions(st: dict, subs: list, keywords: list, args) -> int:
    """拉取订阅并增量并入 state；只有 --refresh-subs 时才调用。返回新增节点数。"""
    for n in st["nodes"].values():
        n["present"] = False
    tf = Table(title="订阅拉取", box=box.SIMPLE_HEAVY)
    for c in ("机场", "原始", "过滤", "在场", "新增", "流量/到期", "状态"):
        tf.add_column(c, justify="left" if c in ("机场", "流量/到期", "状态") else "right")
    name_owner = {n["name"]: k for k, n in st["nodes"].items()}
    total_new = 0
    for s in subs:
        with console.status(f"拉取 [cyan]{s.name}[/cyan] ..."):
            #  proxies, status, info = fetch_sub(s, args.ua)
            proxies, status, info = fetch_sub(s, args.ua, cookie=args.sub_cookie or "",
                                                  engine=args.sub_engine)
        dropped, kept, new = 0, 0, 0
        for p in proxies:
            p, note = sanitize_proxy(p)
            if not all(k in p for k in ("type", "server", "port")):
                dropped += 1
                continue
            orig = str(p.get("name") or f"{p['type']}-{p['server']}")
            hit = is_info_node(orig, keywords)
            if hit:
                dropped += 1
                if args.verbose:
                    console.print(f"[dim]  过滤 {s.name}: {orig} ← 「{hit}」[/dim]")
                continue
            fp = fingerprint(s.name, p)
            base = f"AI-{s.name}-{orig}"
            old = st["nodes"].get(fp, {}).get("name")
            if old and (old == base or old.startswith(base + "#")):
                name = old
            else:
                name, i = base, 2
                while name in name_owner and name_owner[name] != fp:
                    name = f"{base}#{i}"
                    i += 1
            name_owner[name] = fp
            p["name"] = name
            if fp in st["nodes"]:
                n = st["nodes"][fp]
                n.update(present=True, last_seen=now_str(), name=name, proxy=p,
                         provider=s.name, orig_name=orig)
                n["port"] = None
            else:
                st["nodes"][fp] = new_node(s.name, orig, name, p)
                st["nodes"][fp]["port"] = None
                new += 1
            if note:
                log(f"SANITIZE {name}: {note}")
            kept += 1
        total_new += new
        tf.add_row(s.name, str(len(proxies)), str(dropped), str(kept), str(new), info or "-",
                   f"[green]{status}[/green]" if status == "ok" else f"[red]{status}[/red]")
        log(f"SUB {s.name}: raw={len(proxies)} dropped={dropped} kept={kept} new={new} {status}")
        time.sleep(args.sub_interval + random.uniform(0, 1.5))   # 订阅之间礼貌间隔
    console.print(tf)
    return total_new


def run_round(st, subs, keywords, exe, ver, tiers, args) -> int:
    t_start = time.time()
    if args.refresh_subs:
        total_new = refresh_subscriptions(st, subs, keywords, args)
    else:
        total_new = 0
        if args.refresh_only:
            save_state(st, args.state)
            present = sum(1 for n in st["nodes"].values() if n["present"])
            console.print(f"[green]只刷新订阅完成[/green]：在场 {present} 个节点(新增 {total_new})，"
                          f"未测解锁/未采样/未生成yaml。耗时 {time.time()-t_start:.0f}s")
            return 0
        console.print(f"[dim]节点池: 使用 state 缓存 "
                      f"{sum(1 for n in st['nodes'].values() if n['present'])} 个在场节点"
                      f"(不拉订阅；要更新节点池加 --refresh-subs)[/dim]")

    if args.force_unlock:
        cnt = 0
        for n in st["nodes"].values():
            if args.provider and n["provider"] != args.provider:
                continue
            n["services"] = {s: new_service() for s in SERVICES}
            n.update(blocked=False, blocked_reason=None, blocked_at=None,
                     sealed=False, tier=0, kernel_fail=0)
            n["region"].pop("apple", None)
            cnt += 1
        console.print(f"[yellow]--force-unlock: 已重置 {cnt} 个节点的解锁状态[/yellow]")

    if args.reset_stats:
        cnt = 0
        for n in st["nodes"].values():
            if args.provider and n["provider"] != args.provider:
                continue
            n["reach"] = {"samples": 0, "google_ok": 0, "link_samples": 0, "link_ok": 0,
                          "recent": [], "consecutive_fail": 0, "delay_last": None,
                          "delay_ewma": None, "last_ok_at": None}
            n["last_google_ok"] = None
            cnt += 1
        console.print(f"[yellow]--reset-stats: 已清零 {cnt} 个节点的健康统计(解锁结论保留)[/yellow]")

    #  targets = [n for n in st["nodes"].values() if n["present"] and not n["blocked"]]
    targets = [n for n in st["nodes"].values() if n["present"] and not n["blocked"]]

    if args.provider:
        targets = [n for n in targets if n["provider"] == args.provider]
    if args.ipqs_only:                                  # [IPQS]
        targets = [n for n in targets if ipqs_pending(n, args)]
    if args.limit > 0:
        targets = targets[:args.limit]

    if args.ipqs and args.ipqs_block_above is not None and not targets:   # [IPQS]
        for n in st["nodes"].values():
            if n["present"] and not n["blocked"] and (not args.provider or n["provider"] == args.provider):
                maybe_ipqs_block(n, args)

    if not targets:
        if args.ipqs_only:                              # [IPQS]
            console.print("[green]没有待查 IP 纯净度的节点(均已完成或不符合 --ipqs-min-tier)[/green]")
            if args.stats_out:
                dump_stats(st, args.stats_out, args)
            show_ipqs(st, args)
            if args.ipqs_block_above is not None:
                emit(st, tiers, args.out_dir, args)
            save_state(st, args.state)
            return 0
        console.print("[yellow]没有可测节点(全部封盘或订阅为空)，仅重新生成 yaml[/yellow]")
        emit(st, tiers, args.out_dir, args)
        save_state(st, args.state)
        return 2

    if args.ipqs:                                       # [IPQS]
        reset_ip_run_ctx(st, args)
        lip = _IP_RUN_CTX.get("local_ip")
        console.print(f"[dim]本机直连 IP 基线: {lip or '(未取得)'}[/dim]")

    n_unlock = 0 if (args.stability_only or args.ipqs_only) else sum(1 for n in targets if unlock_pending(n))
    n_stab = 0 if (args.no_stability_sample or args.ipqs_only) else len(targets)
    n_ipqs = sum(1 for n in targets if ipqs_pending(n, args)) if args.ipqs else 0

    console.print(f"待处理 [bold]{len(targets)}[/bold] 个节点：解锁增量 {n_unlock}，"
                  f"健康采样 {n_stab}"
                  + (f"，IP纯净度 {n_ipqs}" if args.ipqs else "") + "\n")

    # ---- 分批测试 ----
    workdir = Path(tempfile.mkdtemp(prefix="ai-clean-"))
    lock = Lock()
    done = [0]
    batches = [targets[i:i + args.batch_size] for i in range(0, len(targets), args.batch_size)]
    try:
        with Progress(TextColumn("{task.description}"), BarColumn(),
                      TextColumn("{task.completed}/{task.total}"),
                      TimeElapsedColumn(), TimeRemainingColumn(), console=console) as prog:
            task = prog.add_task("处理中", total=len(targets))

            def on_done(n):
                with lock:
                    done[0] += 1
                    bk = sum(1 for x in targets if x["blocked"])
                    if args.ipqs_only:
                        iq_ok = sum(1 for x in targets if (x.get("ipqs") or {}).get("ok"))
                        desc = f"IP完成:{iq_ok} 封盘:{bk}"
                    else:
                        t1 = sum(1 for x in targets if x["tier"] >= 1)
                        t2 = sum(1 for x in targets if x["tier"] >= 2)
                        t3 = sum(1 for x in targets if x["tier"] >= 3)
                        desc = f"T1:{t1} T2:{t2} T3:{t3} 封盘:{bk}"
                    prog.update(task, advance=1, description=desc)

            for bi, batch in enumerate(batches, 1):
                console.print(f"[dim]批次 {bi}/{len(batches)}: {len(batch)} 节点, "
                              f"{sum(1 for n in batch if needs_listener(n, args))} 个 listener[/dim]")
                run_batch(batch, args, exe, workdir, on_done, budget=[25])
                save_state(st, args.state)          # 每批落盘，崩溃安全
    finally:
        _atexit_cleanup()
        if args.keep_temp:
            console.print(f"[dim]临时目录保留: {workdir}[/dim]")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    for n in st["nodes"].values():
        n["port"] = None
        if n["present"] and args.ipqs and args.ipqs_block_above is not None:   # [IPQS]
            if not args.provider or n["provider"] == args.provider:
                maybe_ipqs_block(n, args)
        if n["present"] and not args.stability_only and not args.ipqs_only:
            finalize(n)

    st["runs"].append({"at": now_str(), "seconds": round(time.time() - t_start),
                       "targets": len(targets), "unlock_work": n_unlock, "new_nodes": total_new,
                       "t1": sum(1 for n in targets if n["tier"] >= 1),
                       "t2": sum(1 for n in targets if n["tier"] >= 2),
                       "t3": sum(1 for n in targets if n["tier"] >= 3)})

    st["runs"] = st["runs"][-200:]
    save_state(st, args.state)
    if args.stats_out:
        dump_stats(st, args.stats_out, args)
    if args.stability_only:
        show_stability(st, args)
        if args.ipqs:                                   # [IPQS]
            show_ipqs(st, args)
        console.print(f"[dim]纯稳定性模式：未动解锁/tier/封盘，未生成yaml。"
                      f"耗时 {time.time()-t_start:.0f}s[/dim]")
        return 0
    if args.ipqs_only:                                  # [IPQS]
        show_ipqs(st, args)
        if args.ipqs_block_above is not None:
            written = emit(st, tiers, args.out_dir, args)
            for t, (p, cnt) in written.items():
                console.print(f"[green]T{t}[/green] {cnt:4d} 节点 → {p}")
        console.print(f"[dim]纯 IP 纯净度模式：未动解锁/tier(--ipqs-block-above 除外)。"
                      f"耗时 {time.time()-t_start:.0f}s[/dim]")
        return 0
    written = emit(st, tiers, args.out_dir, args)
    show_status(st, args)
    if args.ipqs:                                       # [IPQS]
        show_ipqs(st, args)

    tw = Table(title=f"输出 (耗时 {time.time()-t_start:.0f}s)", box=box.SIMPLE_HEAVY)
    tw.add_column("档")
    tw.add_column("节点", justify="right")
    tw.add_column("文件")
    for t, (p, cnt) in written.items():
        tw.add_row(f"T{t}", str(cnt), str(p))
    console.print(tw)
    console.print("[dim]主配置用 type: file 的 proxy-provider 指向这些文件，"
                  "url-test(tolerance:200) + fallback 兜底消费[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
