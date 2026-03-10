import aiohttp
import asyncio
import base64
import ssl
import os
from typing import List
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.all import AstrBotConfig
from astrbot.core.message.components import Image

# ========================================================
# 📦 封装层：RunningHub 官方 API 客户端 (支持多图返回)
# ========================================================
class RunningHubClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.create_url = "https://www.runninghub.cn/task/openapi/create"
        self.outputs_url = "https://www.runninghub.cn/task/openapi/outputs"
        # 新增资源上传接口地址
        self.upload_url = "https://www.runninghub.cn/openapi/v2/media/upload/binary"
        
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

    # ==========================================
    # 🚀 新增：通用媒体上传工具
    # 适用于图片、音频、视频、ZIP 压缩包
    # ==========================================
    async def upload_media(self, file_bytes: bytes, file_name: str = "upload.png") -> str:
        """上传二进制文件并返回相对路径 (fileName)"""
        headers = {
            "Authorization": f"Bearer {self.api_key}"
            # 注意：multipart/form-data 不需要手动设置 Content-Type，aiohttp 会自动处理边界
        }
        connector = aiohttp.TCPConnector(ssl=self.ssl_context)

        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            # 构造表单数据
            form_data = aiohttp.FormData()
            form_data.add_field('file', file_bytes, filename=file_name)
            
            async with session.post(self.upload_url, data=form_data, timeout=60) as resp:
                result = await resp.json()
                if result.get("code") != 0:
                    raise Exception(f"文件上传失败: {result.get('message')} (Code: {result.get('code')})")
                
                # 返回文档要求的相对路径 fileName
                return result["data"]["fileName"]

    async def execute_workflow(self, workflow_id: str, node_info_list: list, use_plus: bool = False, max_retries: int = 60) -> List[bytes]:
        """提交任务并轮询，支持返回多张图片的字节流列表"""
        headers = {
            "Authorization": f"Bearer {self.api_key}", 
            "Content-Type": "application/json"
        }
        connector = aiohttp.TCPConnector(ssl=self.ssl_context)

        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
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

            for _ in range(max_retries):
                await asyncio.sleep(5)
                async with session.post(self.outputs_url, json={"apiKey": self.api_key, "taskId": task_id}, timeout=30) as resp:
                    data = await resp.json()
                    status_code = data.get("code")
                    
                    if status_code == 0 and data.get("data"):
                        images_bytes_list = []
                        for item in data["data"]:
                            img_url = item.get("fileUrl")
                            if img_url:
                                async with session.get(img_url) as img_resp:
                                    if img_resp.status == 200:
                                        images_bytes_list.append(await img_resp.read())
                        
                        if not images_bytes_list:
                            raise Exception("接口已完成，但未成功下载到任何图片")
                        return images_bytes_list
                        
                    elif status_code not in (813, 804):
                        raise Exception(f"生成中断: {data.get('msg')}")

            raise Exception("任务处理超时")

# ========================================================
# 🤖 插件业务层 (模块化解耦)
# ========================================================
@register("astrbot_plugin_runninhubApi_wcy", "YourName", "RunningHub 终极模块化版", "6.0.0")
class WcyTTIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context) 
        self.config = config
        concurrency = max(1, self.config.get("concurrency", 1))
        self.semaphore = asyncio.Semaphore(concurrency)

    # ================= 🛠️ 基建层：可复用扩展工具 =================
    def _parse_prompts(self, prompt_str: str, mode: str) -> tuple:
        """根据不同的模式 (tti, iti) 动态获取对应的默认提示词"""
        default_pos = self.config.get(f"{mode}_default_pos_prompt", "").strip()
        default_neg = self.config.get(f"{mode}_default_neg_prompt", "").strip()

        clean_prompt_str = prompt_str.replace("[图片]", "").strip()

        if not clean_prompt_str:
            return default_pos, default_neg
        
        prompts = clean_prompt_str.split('|', 1)
        pos_prompt = prompts[0].strip() if prompts[0].strip() else default_pos
        neg_prompt = prompts[1].strip() if len(prompts) > 1 else default_neg
        
        return pos_prompt, neg_prompt

    # ================= 🛠️ 基建层：更新为提取二进制 =================
    async def _extract_image_bytes(self, event: AstrMessageEvent, max_count: int = 1) -> List[bytes]:
        """提取用户发送的图片，返回原生二进制数据列表"""
        message_chain = getattr(event.message_obj, "message", [])
        images = [comp for comp in message_chain if isinstance(comp, Image)]
        
        if not images:
            return []

        bytes_list = []
        for img_comp in images[:max_count]:
            img_bytes = b""
            if getattr(img_comp, "url", None) and str(img_comp.url).startswith("http"):
                async with aiohttp.ClientSession() as session:
                    async with session.get(img_comp.url, timeout=30) as resp:
                        if resp.status == 200:
                            img_bytes = await resp.read()
            elif getattr(img_comp, "file", None) or getattr(img_comp, "path", None):
                path = getattr(img_comp, "file", None) or getattr(img_comp, "path", None)
                if path and os.path.exists(path):
                    with open(path, "rb") as f:
                        img_bytes = f.read()
            
            if img_bytes:
                bytes_list.append(img_bytes)

        return bytes_list

    # ================= 🌟 1. 文生图指令 =================
    @filter.command("wcy_tti")
    async def wcy_tti_command(self, event: AstrMessageEvent, *, prompt_str: str = ""):
        '''文生图: /wcy_tti [正向提示词] | [反向提示词]'''
        api_key = self.config.get("api_key", "").strip()
        use_plus = self.config.get("use_plus_api", False)
        
        workflow_id = self.config.get("tti_workflow_id", "").strip()
        pos_node_id = self.config.get("tti_pos_node_id", "").strip()
        neg_node_id = self.config.get("tti_neg_node_id", "").strip()

        if not all([api_key, workflow_id, pos_node_id, neg_node_id]):
            yield event.plain_result("❌ 文生图配置不完整，请检查 WebUI 设置。")
            return

        pos_prompt, neg_prompt = self._parse_prompts(prompt_str, mode="tti")

        node_info_list = [{"nodeId": pos_node_id, "fieldName": "text", "fieldValue": pos_prompt}]
        if neg_prompt:
            node_info_list.append({"nodeId": neg_node_id, "fieldName": "text", "fieldValue": neg_prompt})

        queue_msg = "\n🚦 正在为您排队..." if self.semaphore.locked() else ""
        
        async def background_task():
            async with self.semaphore:
                client = RunningHubClient(api_key=api_key)
                try:
                    # 接收字节流列表
                    img_bytes_list = await client.execute_workflow(workflow_id, node_info_list, use_plus=use_plus)
                    # 组装多个图片组件
                    image_components = [Image.fromBytes(b) for b in img_bytes_list]
                    # 一次性发送所有生成的图片
                    await event.send(event.chain_result(image_components))
                except Exception as e:
                    logger.error(f"文生图错误: {e}")
                    await event.send(event.plain_result(f"💥 文生图失败: {str(e)}"))

        asyncio.create_task(background_task())
        yield event.plain_result(f"⏳ [文生图] 已加入后台队列{' (Plus模式)' if use_plus else ''}...{queue_msg}")

   # ================= 🌟 2. 单图生图指令 =================
    @filter.command("wcy_single_iti")
    async def wcy_iti_command(self, event: AstrMessageEvent, *, prompt_str: str = ""):
        '''图生图: /wcy_single_iti [正向提示词] | [反向提示词] [附带图片]'''
        api_key = self.config.get("api_key", "").strip()
        use_plus = self.config.get("use_plus_api", False)
        
        workflow_id = self.config.get("iti_workflow_id", "").strip()
        img_node_id = self.config.get("iti_image_node_id", "").strip()
        pos_node_id = self.config.get("iti_pos_node_id", "").strip()
        neg_node_id = self.config.get("iti_neg_node_id", "").strip()

        if not all([api_key, workflow_id, img_node_id, pos_node_id, neg_node_id]):
            yield event.plain_result("❌ 单图生图配置不完整，请检查 WebUI 设置。")
            return

        yield event.plain_result("📥 正在读取并上传源图片，请稍候...")
        
        # 1. 获取纯二进制图片
        image_bytes_list = await self._extract_image_bytes(event, max_count=1)
        if not image_bytes_list:
            yield event.plain_result("❌ 请在发送指令时附带一张图片！\n示例：/wcy_single_iti 赛博朋克 | 模糊 [图片]")
            return
            
        img_bytes = image_bytes_list[0]
        pos_prompt, neg_prompt = self._parse_prompts(prompt_str, mode="iti")
        client = RunningHubClient(api_key=api_key)

        queue_msg = "\n🚦 正在为您排队..." if self.semaphore.locked() else ""

        async def background_task():
            async with self.semaphore:
                try:
                    # 2. 先调用上传接口，获取服务器相对路径
                    # 这里可以根据文件头简单判断一下后缀
                    file_ext = "jpg" if img_bytes.startswith(b'\xff\xd8\xff') else "png"
                    uploaded_file_name = await client.upload_media(img_bytes, file_name=f"input_image.{file_ext}")
                    
                    # 3. 构造节点数据，填入服务器返回的 fileName
                    node_info_list = [
                        {"nodeId": img_node_id, "fieldName": "image", "fieldValue": uploaded_file_name},
                        {"nodeId": pos_node_id, "fieldName": "text", "fieldValue": pos_prompt}
                    ]
                    if neg_prompt:
                        node_info_list.append({"nodeId": neg_node_id, "fieldName": "text", "fieldValue": neg_prompt})

                    # 4. 提交工作流
                    res_bytes_list = await client.execute_workflow(workflow_id, node_info_list, use_plus=use_plus)
                    image_components = [Image.fromBytes(b) for b in res_bytes_list]
                    await event.send(event.chain_result(image_components))
                except Exception as e:
                    logger.error(f"图生图错误: {e}")
                    await event.send(event.plain_result(f"💥 图生图失败: {str(e)}"))

        asyncio.create_task(background_task())
        yield event.plain_result(f"⏳ [单图生图] 已加入后台队列{' (Plus模式)' if use_plus else ''}...{queue_msg}")