# -*- coding: utf-8 -*-
import urllib.request
import json
import os
import time
import subprocess
import re
import sys

# 强制设置标准输出为 UTF-8，防止某些极端环境下的系统级编码崩溃
if sys.stdout.encoding != 'UTF-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

CONF_FILE = "/opt/kui/config.json"
SINGBOX_CONF_PATH = "/etc/sing-box/config.json"

try:
    with open(CONF_FILE, 'r') as f:
        env = json.load(f)
except Exception:
    # 修复：改为纯英文输出，彻底杜绝 systemd 下的 UnicodeEncodeError
    print("Failed to read config file. Please check the installation process.")
    exit(1)

API_URL = env["api_url"]
REPORT_URL = env["report_url"]
VPS_IP = env["ip"]
TOKEN = env["token"]

HEADERS = {
    'Content-Type': 'application/json',
    'Authorization': TOKEN,
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
}

# 全局状态字典
last_reported_bytes = {}
argo_tunnels = {}

# ===============================================
# Argo 全自动穿透核心模块
# ===============================================
def ensure_cloudflared():
    if not os.path.exists("/usr/local/bin/cloudflared"):
        # 修复：改为纯英文输出
        print("First Argo node detected. Installing cloudflared silently...")
        os.system("curl -L -o /usr/local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64")
        os.system("chmod +x /usr/local/bin/cloudflared")

def start_argo_tunnel(port):
    ensure_cloudflared()
    cmd = ["/usr/local/bin/cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}"]
    # 后台挂起运行，并捕获 stderr
    p = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    
    url = None
    start_time = time.time()
    # 限制 15 秒抓取，防止阻塞主进程
    while time.time() - start_time < 15:
        line = p.stderr.readline()
        if not line:
            break
        # 正则精准打击临时域名
        match = re.search(r'https://([a-zA-Z0-9-]+\.trycloudflare\.com)', line)
        if match:
            url = match.group(1)
            break
    return p, url

def process_argo_nodes(configs):
    argo_urls_to_report = []
    expected_argo_ports = []
    
    for node in configs:
        if node.get('protocol') == 'VLESS-Argo':
            port = str(node['port'])
            expected_argo_ports.append(port)
            
            # 建立新隧道
            if port not in argo_tunnels:
                p, url = start_argo_tunnel(port)
                if url:
                    argo_tunnels[port] = {"proc": p, "url": url}
                    argo_urls_to_report.append({"id": node["id"], "url": url})
            else:
                # 维持旧隧道状态回传
                argo_urls_to_report.append({"id": node["id"], "url": argo_tunnels[port]["url"]})
                
    # 清理在面板上已被删除的僵尸隧道
    for port in list(argo_tunnels.keys()):
        if port not in expected_argo_ports:
            argo_tunnels[port]["proc"].terminate()
            del argo_tunnels[port]
            
    return argo_urls_to_report


# ===============================================
# 系统监控与流量抓取模块
# ===============================================
def get_system_status():
    try:
        cpu = float(os.popen("top -bn1 | grep load | awk '{printf \"%.2f\", $(NF-2)}'").read().strip())
        mem = float(os.popen("free -m | awk 'NR==2{printf \"%.2f\", $3*100/$2 }'").read().strip())
        return {"cpu": int(cpu), "mem": mem}
    except Exception:
        return {"cpu": 0, "mem": 0}

def get_port_traffic(port, protocol="tcp"):
    try:
        check_in = f"iptables -C INPUT -p {protocol} --dport {port}"
        if subprocess.run(check_in, shell=True, stderr=subprocess.DEVNULL).returncode != 0:
            subprocess.run(f"iptables -I INPUT -p {protocol} --dport {port}", shell=True)

        check_out = f"iptables -C OUTPUT -p {protocol} --sport {port}"
        if subprocess.run(check_out, shell=True, stderr=subprocess.DEVNULL).returncode != 0:
            subprocess.run(f"iptables -I OUTPUT -p {protocol} --sport {port}", shell=True)

        out_in = subprocess.check_output(f"iptables -nvx -L INPUT | grep 'dpt:{port}'", shell=True).decode()
        in_bytes = sum([int(line.split()[1]) for line in out_in.strip().split('\n') if line])

        out_out = subprocess.check_output(f"iptables -nvx -L OUTPUT | grep 'spt:{port}'", shell=True).decode()
        out_bytes = sum([int(line.split()[1]) for line in out_out.strip().split('\n') if line])

        return in_bytes + out_bytes
    except Exception:
        return 0


# ===============================================
# 云端通讯模块
# ===============================================
def report_status(current_nodes, argo_urls):
    global last_reported_bytes
    status = get_system_status()
    status["ip"] = VPS_IP
    status["argo_urls"] = argo_urls  # 注入抓取到的 Argo 域名
    
    node_traffic_deltas = []
    current_ids = set()

    for node in current_nodes:
        nid = node["id"]
        port = node["port"]
        current_ids.add(nid)
        # Hysteria2 和 TUIC 必须抓 UDP 流量
        proto = "udp" if node["protocol"] in ["Hysteria2", "TUIC"] else "tcp"
        
        current_bytes = get_port_traffic(port, proto)
        last_bytes = last_reported_bytes.get(nid, 0)
        
        delta = current_bytes - last_bytes
        if delta < 0: delta = current_bytes
        if delta > 0: node_traffic_deltas.append({ "id": nid, "delta_bytes": delta })
        
        last_reported_bytes[nid] = current_bytes

    for old_id in list(last_reported_bytes.keys()):
        if old_id not in current_ids:
            del last_reported_bytes[old_id]

    status["node_traffic"] = node_traffic_deltas

    req = urllib.request.Request(REPORT_URL, data=json.dumps(status).encode('utf-8'), headers=HEADERS)
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

def fetch_and_apply_configs():
    req = urllib.request.Request(f"{API_URL}?ip={VPS_IP}", headers=HEADERS)
    try:
        res = urllib.request.urlopen(req, timeout=10)
        data = json.loads(res.read().decode('utf-8'))
        if data.get("success"):
            nodes = data.get("configs", [])
            build_singbox_config(nodes)
            return nodes
    except Exception:
        pass
    return None


# ===============================================
# Sing-box 全协议底层配置引擎
# ===============================================
def build_singbox_config(nodes):
    singbox_config = {
        "log": {"level": "warn"},
        "inbounds": [],
        "outbounds": [{"type": "direct", "tag": "direct-out"}],
        "route": {"rules": []}
    }

    active_certs = []

    for node in nodes:
        in_tag = f"in-{node['id']}"
        proto = node["protocol"]
        
        if proto == "VLESS":
            singbox_config["inbounds"].append({
                "type": "vless", "tag": in_tag, "listen": "::", "listen_port": int(node["port"]),
                "users": [{"uuid": node["uuid"]}]
            })
            
        elif proto == "Reality":
            singbox_config["inbounds"].append({
                "type": "vless", "tag": in_tag, "listen": "::", "listen_port": int(node["port"]),
                "users": [{"uuid": node["uuid"], "flow": "xtls-rprx-vision"}],
                "tls": {
                    "enabled": True, "server_name": node["sni"],
                    "reality": {
                        "enabled": True, "handshake": {"server": node["sni"], "server_port": 443},
                        "private_key": node["private_key"], "short_id": [node["short_id"]]
                    }
                }
            })

        elif proto in ["Hysteria2", "TUIC"]:
            # 自动化自签证书模块复用
            cert_path = f"/opt/kui/cert_{node['id']}.pem"
            key_path = f"/opt/kui/key_{node['id']}.pem"
            sni = node.get("sni", "www.apple.com")
            
            active_certs.extend([f"cert_{node['id']}.pem", f"key_{node['id']}.pem"])

            if not os.path.exists(cert_path) or not os.path.exists(key_path):
                cmd = f'openssl req -x509 -nodes -newkey ec:<(openssl ecparam -name prime256v1) -keyout {key_path} -out {cert_path} -days 3650 -subj "/O=GlobalSign/CN={sni}" 2>/dev/null'
                subprocess.run(cmd, shell=True, executable='/bin/bash')
                subprocess.run(["chmod", "644", cert_path, key_path])

            if proto == "Hysteria2":
                singbox_config["inbounds"].append({
                    "type": "hysteria2", "tag": in_tag, "listen": "::", "listen_port": int(node["port"]),
                    "users": [{"password": node["uuid"]}],
                    "tls": { "enabled": True, "alpn": ["h3"], "certificate_path": cert_path, "key_path": key_path }
                })
            elif proto == "TUIC":
                singbox_config["inbounds"].append({
                    "type": "tuic", "tag": in_tag, "listen": "::", "listen_port": int(node["port"]),
                    "users": [{"uuid": node["uuid"], "password": node["private_key"]}],
                    "tls": { "enabled": True, "alpn": ["h3"], "certificate_path": cert_path, "key_path": key_path }
                })

        elif proto == "VLESS-Argo":
            # 必须且只能监听本地地址 127.0.0.1，防止公网暴露，交由 cloudflared 穿透
            singbox_config["inbounds"].append({
                "type": "vless", "tag": in_tag, "listen": "127.0.0.1", "listen_port": int(node["port"]),
                "users": [{"uuid": node["uuid"]}],
                "transport": {"type": "ws", "path": "/"}
            })
            
        elif proto == "Socks5":
            singbox_config["inbounds"].append({
                "type": "socks", "tag": in_tag, "listen": "::", "listen_port": int(node["port"]),
                "users": [{"username": node["uuid"], "password": node["private_key"]}]
            })
            
        elif proto == "dokodemo-door":
            singbox_config["inbounds"].append({ "type": "direct", "tag": in_tag, "listen": "::", "listen_port": int(node["port"]) })
            out_tag = f"out-{node['id']}"
            
            if node.get("relay_type") == "internal" and node.get("chain_target"):
                t = node["chain_target"]
                outbound = { "type": t["protocol"].lower(), "tag": out_tag, "server": t["ip"], "server_port": int(t["port"]), "uuid": t["uuid"] }
                if t["protocol"] == "Reality":
                    outbound["tls"] = { "enabled": True, "server_name": t["sni"], "reality": { "enabled": True, "public_key": t["public_key"], "short_id": t["short_id"] } }
                singbox_config["outbounds"].append(outbound)
            else:
                singbox_config["outbounds"].append({ "type": "direct", "tag": out_tag, "override_address": node["target_ip"], "override_port": int(node["target_port"]) })
            
            singbox_config["route"]["rules"].append({ "inbound": [in_tag], "outbound": out_tag })

    # 智能清理废弃节点的证书文件
    try:
        for filename in os.listdir("/opt/kui/"):
            if (filename.startswith("cert_") or filename.startswith("key_")) and filename.endswith(".pem"):
                if filename not in active_certs:
                    os.remove(os.path.join("/opt/kui/", filename))
    except Exception:
        pass

    new_config_str = json.dumps(singbox_config, indent=2)
    old_config_str = ""
    if os.path.exists(SINGBOX_CONF_PATH):
        with open(SINGBOX_CONF_PATH, "r") as f:
            old_config_str = f.read()

    # 热重载判定
    if new_config_str != old_config_str:
        with open(SINGBOX_CONF_PATH, "w") as f:
            f.write(new_config_str)
        subprocess.run(["systemctl", "restart", "sing-box"])


# ===============================================
# 主循环引擎
# ===============================================
if __name__ == "__main__":
    current_active_nodes = []
    while True:
        # 拉取并应用最新节点配置
        fetched_nodes = fetch_and_apply_configs()
        if fetched_nodes is not None:
            current_active_nodes = fetched_nodes
            
        # 守护 Argo 穿透隧道，抓取最新 URL
        argo_urls = process_argo_nodes(current_active_nodes)
        
        # 将流量和 Argo 域名汇报给 Cloudflare 数据库
        report_status(current_active_nodes, argo_urls)
        
        time.sleep(60)
