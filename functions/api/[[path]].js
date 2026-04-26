export async function onRequest(context) {
    const { request, env, params } = context;
    const url = new URL(request.url);
    const method = request.method;
    const action = params.path ? params.path[0] : ''; 

    // 初始化获取全量数据
    let vpsList = await env.KUI_KV.get("vps_list", { type: "json" }) || [];
    let nodeList = await env.KUI_KV.get("node_list", { type: "json" }) || [];

    try {
        // --- 1. 面板前端获取合并数据 ---
        if (action === "data" && method === "GET") {
            return Response.json({ servers: vpsList, nodes: nodeList });
        }

        // --- 2. VPS (服务器) 管理 ---
        if (action === "vps") {
            if (method === "POST") {
                const newVps = await request.json();
                // 确保 IP 不重复
                if (!vpsList.find(v => v.ip === newVps.ip)) {
                    vpsList.push({ ip: newVps.ip, name: newVps.name, cpu: 0, mem: 0, last_report: null });
                    await env.KUI_KV.put("vps_list", JSON.stringify(vpsList));
                }
                return Response.json({ success: true });
            }
            if (method === "DELETE") {
                const targetIp = url.searchParams.get("ip");
                vpsList = vpsList.filter(v => v.ip !== targetIp);
                // 级联删除该机器下的节点
                nodeList = nodeList.filter(n => n.vps_ip !== targetIp); 
                await env.KUI_KV.put("vps_list", JSON.stringify(vpsList));
                await env.KUI_KV.put("node_list", JSON.stringify(nodeList));
                return Response.json({ success: true });
            }
        }

        // --- 3. 节点配置管理 ---
        if (action === "nodes") {
            if (method === "POST") {
                const newNode = await request.json();
                nodeList.push(newNode);
                await env.KUI_KV.put("node_list", JSON.stringify(nodeList));
                return Response.json({ success: true });
            }
            if (method === "DELETE") {
                const id = url.searchParams.get("id");
                nodeList = nodeList.filter(n => n.id !== id);
                await env.KUI_KV.put("node_list", JSON.stringify(nodeList));
                return Response.json({ success: true });
            }
        }

        // --- 4. 边缘机器拉取配置专用接口 (无需鉴权) ---
        if (action === "config" && method === "GET") {
            const ip = url.searchParams.get("ip");
            const machineNodes = nodeList.filter(n => n.vps_ip === ip);
            return Response.json({ success: true, configs: machineNodes });
        }

        // --- 5. 探针数据上报专用接口 (由 VPS 上的 Agent 脚本定时 POST 调用) ---
        if (action === "report" && method === "POST") {
            const data = await request.json(); 
            // 期望收到的 payload: { ip: "8.8.8.8", cpu: 15, mem: 42 }
            
            let updated = false;
            for (let i = 0; i < vpsList.length; i++) {
                if (vpsList[i].ip === data.ip) {
                    vpsList[i].cpu = data.cpu;
                    vpsList[i].mem = data.mem;
                    vpsList[i].last_report = Date.now(); // 刷新在线心跳时间
                    updated = true;
                    break;
                }
            }
            
            if (updated) {
                // 注意：高频写入 KV 可能会触发免费版限制，建议 VPS 探针上报频率设为 3-5 分钟一次
                await env.KUI_KV.put("vps_list", JSON.stringify(vpsList));
            }
            return Response.json({ success: true });
        }

        return new Response("Not Found API Route", { status: 404 });
        
    } catch (err) {
        return Response.json({ error: err.message }, { status: 500 });
    }
}
