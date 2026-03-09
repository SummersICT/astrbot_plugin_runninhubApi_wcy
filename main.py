import aiohttp
import asyncio
import ssl
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.all import AstrBotConfig
from astrbot.core.message.components import Image

# ========================================================
# 📦 封装层：RunningHub 官方 API 客户端 (保持不变)
# ========================================================
class RunningHubClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.create_url = "https://www.runninghub.cn/task/openapi/create"
        self.outputs_url = "https://www.runninghub.cn/task/openapi/outputs"
        
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

    async def execute_workflow(self, workflow_id: str, node_info_list: list, use_plus: bool = False, max_retries: int = 60) -> bytes:
        headers = {
            "Authorization": f"Bearer {self.api_key}", 
            "Content-Type": "application/json"
        }
        connector = aiohttp.TCPConnector(ssl=self.ssl_context)

        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            # 1. 提交任务
            payload = {
                "apiKey": self.api_key,
                "workflowId": workflow_id,
                "nodeInfoList": node_info_list,
                "instanceType": "plus" if use_plus else "medium"
            }
            
            async with session.post(self.create_url, json=payload, timeout=30) as resp:
                create_data = await resp.json()
                if create_data.get("code") != 0:
                    raise Exception(f"任务提交失败: {create_data.get('msg')} (Code: {create_data.get('code')})")
                task_id = create_data.get("data", {}).get("taskId")

            # 2. 轮询获取结果
            for _ in range(max_retries):
                await asyncio.sleep(5)
                async with session.post(self.outputs_url, json={"apiKey": self.api_key, "taskId": task_id}, timeout=30) as resp:
                    data = await resp.json()
                    status_code = data.get("code")
                    
                    if status_code == 0 and data.get("data"):
                        img_url = data["data"][0]["fileUrl"]
                        async with session.get(img_url) as img_resp:
                            if img_resp.status != 200:
                                raise Exception("图片源文件下载失败")
                            return await img_resp.read()
                    elif status_code not in (813, 804):
                        raise Exception(f"生成中断: {data.get('msg')}")

            raise Exception("任务处理超时")

# ========================================================
# 🤖 插件业务层 (重构：采用后台异步任务)
# ========================================================
@register("astrbot_plugin_runninhubApi_wcy", "YourName", "RunningHub 异步后台版", "3.0.0")
class WcyTTIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context) 
        self.config = config
        concurrency = max(1, self.config.get("concurrency", 1))
        self.semaphore = asyncio.Semaphore(concurrency)

    @filter.command("wcy_tti")
    async def wcy_tti_command(self, event: AstrMessageEvent, *, prompt_str: str = ""):
        # 1. 获取配置
        api_key = self.config.get("api_key", "").strip()
        workflow_id = self.config.get("workflow_id", "").strip()
        pos_node_id = self.config.get("pos_prompt_node_id", "").strip()
        neg_node_id = self.config.get("neg_prompt_node_id", "").strip()
        use_plus = self.config.get("use_plus_api", False)

        if not all([api_key, workflow_id, pos_node_id, neg_node_id]):
            yield event.plain_result("❌ 插件配置不完整，请检查 WebUI 设置。")
            return

        # 2. 智能提示词解析
        default_pos = self.config.get("default_pos_prompt", "").strip()
        default_neg = self.config.get("default_neg_prompt", "").strip()

        if not prompt_str.strip():
            pos_prompt, neg_prompt = default_pos, default_neg
        else:
            prompts = prompt_str.split('|', 1)
            pos_prompt = prompts[0].strip() if prompts[0].strip() else default_pos
            neg_prompt = prompts[1].strip() if len(prompts) > 1 else default_neg

        # 构造节点数据
        node_info_list = [{"nodeId": pos_node_id, "fieldName": "text", "fieldValue": pos_prompt}]
        if neg_prompt:
            node_info_list.append({"nodeId": neg_node_id, "fieldName": "text", "fieldValue": neg_prompt})

        # ========================================================
        # 🚀 核心优化：定义后台执行任务
        # ========================================================
        async def background_generate_task():
            async with self.semaphore:
                client = RunningHubClient(api_key=api_key)
                try:
                    # 默默在后台生成
                    img_bytes = await client.execute_workflow(workflow_id, node_info_list, use_plus=use_plus)
                    # 🌟 生成完毕后，主动向刚才的对话框推送结果！
                    await event.send(event.chain_result([Image.fromBytes(img_bytes)]))
                except Exception as e:
                    logger.error(f"RunningHub 错误: {e}")
                    # 生成失败也会推送报错信息
                    await event.send(event.plain_result(f"💥 {pos_prompt[:10]}... 生成失败: {str(e)}"))

        # ========================================================
        
        # 判断队列情况，如果队列满了提醒一下
        queue_msg = "\n🚦 正在为您排队..." if self.semaphore.locked() else ""
        
        # 3. 把任务丢到后台执行（不阻塞当前主线程）
        asyncio.create_task(background_generate_task())

        # 4. 瞬间回复用户，并结束当前指令！彻底释放 AstrBot 上下文锁！
        yield event.plain_result(f"⏳ 已加入后台生成队列{' (Plus模式)' if use_plus else ''}...\n你可以继续和机器人聊天，画完会自动发给你！{queue_msg}")