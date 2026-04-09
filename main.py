import aiohttp
import asyncio
import ssl
import os
import uuid
import re
import time
import json
from typing import List, Tuple
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.all import AstrBotConfig
from astrbot.core.message.components import Image

# ========================================================
# 🛠️ 1. 消息来源工具类
# ========================================================
class MessageSourceUtil:
    @staticmethod
    def get_source_info(event: AstrMessageEvent) -> dict:
        msg_obj = event.message_obj
        group_id = getattr(msg_obj, "group_id", None)
        user_id = event.get_sender_id()
        
        sender_name = "User"
        try:
            if hasattr(msg_obj, 'raw_event') and msg_obj.raw_event:
                sender_data = msg_obj.raw_event.get("sender", {})
                sender_name = sender_data.get("nickname") or sender_data.get("card") or "User"
        except Exception as e:
            logger.debug(f"提取昵称失败: {e}")

        return {
            "isGroup": bool(group_id),
            "groupId": str(group_id) if group_id else "",
            "userId": str(user_id),
            "senderName": sender_name,
            "type": "GROUP" if group_id else "PRIVATE"
        }

# ========================================================
# 📦 2. API 客户端 (适配 Webhook 异步模式)
# ========================================================
class RunningHubClient:
    def __init__(self, api_url: str):
        self.api_url = api_url
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

    async def execute_task(self, payload: dict) -> dict:
        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        headers = {"Content-Type": "application/json"}
        
        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            logger.info(f"🚀 发送请求到 Java 后端: {self.api_url}")
            async with session.post(self.api_url, json=payload, timeout=120) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"Java 后端响应异常: {resp.status} - {text}")
                
                result = await resp.json()
                if result.get("code") != 200:
                    raise Exception(f"Java 业务报错: {result.get('msg')}")
                
                outer_data = result.get("data", {}) 
                task_id = None
                if isinstance(outer_data, dict):
                    task_id = outer_data.get("taskId")
                    if not task_id and isinstance(outer_data.get("data"), dict):
                        task_id = outer_data.get("data").get("taskId")

                if task_id:
                    logger.info(f"🎯 成功解析出 TaskId: {task_id}")
                    return {"taskId": task_id}
                
                logger.error(f"❌ 原始结构: {result}")
                raise Exception("无法从 Java 响应中解析出 taskId")

# ========================================================
# 🤖 3. 插件业务层
# ========================================================
@register("astrbot_plugin_runninhubApi_wcy", "summers", "RunningHub 核心异步插件", "2.4.0")
class WcyTTIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context) 
        self.config = config
        concurrency = max(1, self.config.get("concurrency", 1))
        self.semaphore = asyncio.Semaphore(concurrency)
        logger.info("🌟 RunningHub 异步插件已就绪！")

    # ----------------- 🛠️ 内部工具 -----------------
    
    async def _extract_image_bytes(self, event: AstrMessageEvent, max_count: int = 1) -> List[bytes]:
        message_chain = getattr(event.message_obj, "message", [])
        images = [comp for comp in message_chain if isinstance(comp, Image)]
        if not images: return []
        bytes_list = []
        for img_comp in images[:max_count]:
            url = getattr(img_comp, "url", None)
            if url and str(url).startswith("http"):
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=30) as resp:
                        if resp.status == 200: bytes_list.append(await resp.read())
        return bytes_list

    async def _save_local_image(self, image_bytes: bytes, local_save_dir: str, server_base_url: str) -> str:
        # 1. 确保目录存在
        if not os.path.exists(local_save_dir):
            try:
                os.makedirs(local_save_dir, exist_ok=True)
                logger.info(f"📂 目录不存在，已自动创建: {os.path.abspath(local_save_dir)}")
            except Exception as e:
                logger.error(f"❌ 无法创建图片保存目录: {e}")
                raise

        # 2. 准备文件路径
        file_name = f"up_{uuid.uuid4().hex[:8]}_{int(time.time())}.png"
        file_path = os.path.join(local_save_dir, file_name)
        abs_path = os.path.abspath(file_path)

        # 3. 写入文件并校验
        try:
            with open(file_path, "wb") as f:
                f.write(image_bytes)
            
            if os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                logger.info(f"💾 图片转存成功！\n   📍 绝对路径: {abs_path}\n   ⚖️ 文件大小: {file_size} bytes")
            else:
                raise Exception("文件写入校验失败，文件未生成。")
        except Exception as e:
            logger.error(f"❌ 写入文件至磁盘失败: {e}")
            raise

        return server_base_url.rstrip("/") + "/" + file_name

    def _parse_prompt(self, prompt_str: str) -> Tuple[str, str]:
        clean_prompt = prompt_str.replace("[图片]", "").strip()
        pos_match = re.search(r"正\[(.*?)\]", clean_prompt, re.DOTALL)
        neg_match = re.search(r"反\[(.*?)\]", clean_prompt, re.DOTALL)
        return (pos_match.group(1).strip() if pos_match else "", 
                neg_match.group(1).strip() if neg_match else "")

    def _build_request_body(self, config_data: dict, api_key: str, taskWebHook: str, action: str, 
                             node_overrides: dict, params: dict, source_info: dict) -> dict:
        payload = {
            "workflowInfo": {
                "apiKey": api_key,
                "taskWebHook": taskWebHook,
                "workflowId": config_data.get("workflowId", ""),
                "instanceType": "plus" if config_data.get("instanceType") else "standard",
                "nodeInfoList": []
            },
            "action": action,
            "params": params,
            "sourceInfo": source_info
        }

        for key, value in config_data.items():
            if isinstance(value, dict) and "nodeId" in value:
                node_conf = value
                if node_conf.get("nodeId"):
                    node_item = {
                        "nodeType": str(node_conf.get("nodeType", "")),
                        "nodeId": str(node_conf.get("nodeId", "")),
                        "fieldName": str(node_conf.get("fieldName", "")),
                        "fieldValue": str(node_conf.get("fieldValue", ""))
                    }
                    if key in node_overrides:
                        node_item.update(node_overrides[key])
                    payload["workflowInfo"]["nodeInfoList"].append(node_item)
        
        return payload

    # ----------------- 🌟 指令实现 -----------------

    @filter.command("wcy_single_iti")
    async def wcy_iti_command(self, event: AstrMessageEvent, *, prompt_str: str = ""):
        logger.info(f"!!!! 拦截到单图生图指令: {prompt_str} !!!!")
        
        api_key = self.config.get("apiKey", "").strip()
        local_dir = self.config.get("localSaveDir", "").strip()
        base_url = self.config.get("serverBaseUrl", "").strip()
        dispatch_url = self.config.get("apiDispatchUrl", "").strip()
        taskWebHook = self.config.get("taskWebHook", "").strip()
        iti_config = self.config.get("singleImageToImage", {})

        source_info = MessageSourceUtil.get_source_info(event)
        
        # 1. 图片检查
        image_bytes_list = await self._extract_image_bytes(event, max_count=1)
        if not image_bytes_list:
            yield event.plain_result("📢 哎呀！你是不是忘发图片啦？\n✨ 请在发送指令的时候顺便塞给我一张图哦~")
            return

        pos, neg = self._parse_prompt(prompt_str)
        
        async def background_task():
            async with self.semaphore:
                try:
                    # 2. 保存图片
                    image_web_url = await self._save_local_image(image_bytes_list[0], local_dir, base_url)
                    
                    # 3. 构造 Payload
                    payload = self._build_request_body(
                        config_data=iti_config,
                        api_key=api_key,
                        taskWebHook=taskWebHook,
                        action="runningHub:singleImageToImage",
                        node_overrides={"imageNode": {"fileUrl": image_web_url}},
                        params={"positivePrompt": pos, "negativePrompt": neg},
                        source_info=source_info
                    )

                    logger.info(f"🚀 任务提交中，Payload 预览: {json.dumps(payload, ensure_ascii=False)[:200]}...")

                    # 4. 执行请求
                    client = RunningHubClient(dispatch_url)
                    task_data = await client.execute_task(payload)
                    logger.info(f"✅ 成功提交至后端！TaskId: {task_data.get('taskId')}")
                    
                except Exception as e:
                    logger.error(f"⚠️ 流程异常: {e}")
                    error_msg = f"呜呜... 画笔突然断掉啦！💦\n原因：{str(e)}\n请检查配置或稍后再试吧~"
                    await event.send(event.plain_result(error_msg))

        # 启动后台任务
        asyncio.create_task(background_task())
        
        # 5. 立即反馈
        welcome_msg = (
            "🎯 收到啦！画师已就位~\n"
            "🎨 正在全力创作你的 3D 手办图中...\n"
            "🍭 这是一个异步任务，画好后我会立刻飞奔过来送给你的，请耐心等我一下下哦！"
        )
        yield event.plain_result(welcome_msg)