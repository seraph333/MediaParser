import cv2
import numpy as np
import tempfile
import asyncio
import json
import re
import tomllib
import base64
import aiohttp
import io
from PIL import Image
from loguru import logger
from typing import Optional, List
import os
import hashlib
import traceback
from WechatAPI import WechatAPIClient
from utils.plugin_base import PluginBase
from utils.decorators import on_text_message
import random
import html

class MediaParser(PluginBase):
    """
    短视频/图集解析插件（支持抖音、快手），通过配置的接口解析内容并发送到微信
    """

    description = "短视频/图集解析插件（支持抖音、快手），解析内容并发送到微信"
    author = "酱爆"
    version = "1.1.1"
    #1.1.1 7.23 1.修复接口响应内容为空时的报错。2.新增api_timeout，API解析专用超时时间
    #1.1.0 7.23 龙珠接口返回的链接无法在微信内直接打开，发送卡片消息时，均改为使用api_url
    #1.0.9 7.23 1.插件改名为MediaParser，功能更通用。2.移除无用的commands指令。3.优化配置加载路径，使其更健壮。4.优化错误提示，对用户更友好。5.新增龙珠接口
    #1.0.8 7.22 1.优化请求头，提升下载视频成功率。2.如果封面图下载失败，提取视频第一帧作为视频封面。3.修复卡片消息封面图空白
    #1.0.7 7.6 增加快手视频解析功能，通用化处理逻辑
    #1.0.6 7.6 修改图片保存&命名方式，与dify的图片一致以便识图

    def __init__(self):
        super().__init__()
        # 为 aiohttp 创建一个可复用的会话，显著提升网络性能
        self.session = aiohttp.ClientSession()
        
        self.files_dir = "files"
        os.makedirs(self.files_dir, exist_ok=True) # 确保目录存在

        try:
            # 优化点：动态获取配置文件路径，更健壮
            config_path = os.path.join(os.path.dirname(__file__), "config.toml")
            with open(config_path, "rb") as f:
                plugin_config = tomllib.load(f)
            
            # 使用新的类名读取配置
            config = plugin_config["MediaParser"]
            self.enable = config["enable"]
            self.video_api_url = config.get("video_api_url", "")
            self.api_url = config.get("api_url", "")
            self.timeout = config.get("timeout", 30)
            # 优化：加载专用的API超时设置
            self.api_timeout = config.get("api_timeout", 10) 
            self.max_retries = config.get("max_retries", 3)
            self.max_video_size = config.get("max_video_size", 25)

            logger.info("MediaParser 插件配置加载成功")
            if not self.video_api_url and not self.api_url:
                logger.warning("MediaParser 警告: 配置文件中未提供任何API URL。")

        except FileNotFoundError:
            logger.error(f"MediaParser 插件配置文件未找到 ({config_path})，插件已禁用。")
            self.enable = False
        except Exception as e:
            logger.exception(f"MediaParser 插件初始化失败: {e}")
            self.enable = False

    def _extract_video_url(self, text: str) -> Optional[str]:
        """
        从文本中提取抖音或快手视频URL
        
        Args:
            text (str): 包含视频链接的文本
            
        Returns:
            Optional[str]: 提取到的URL，如果未找到则返回None
        """
        # 匹配抖音和快手链接格式
        patterns = [
            r"(https?://v\.douyin\.com/[^\s]+)",      # 抖音短链接
            r"(https?://[^\s]+douyin\.com/[^\s]+)",  # 其他抖音链接
            r"(https?://[^\s]+iesdouyin\.com/[^\s]+)",# 抖音国际版
            r"(ygo:/[^\s]+)",                      # 抖音短码格式
            r"(https?://[^\s]*kuaishou\.com[^\s]*)", # 快手链接 (新)
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                url = match.group(0)
                # 如果是抖音短码格式，转换为完整链接
                if url.startswith("ygo:/"):
                    return f"https://v.douyin.com/{url[5:].strip()}"
                return url
        return None

    def _parse_new_api_response(self, json_response: dict) -> Optional[dict]:
        """[新增] 解析新接口 (video_api_url) 的返回数据并转换为标准格式"""
        # 修复：增加对空响应的检查
        if not json_response:
            return {"error": "新API返回了空响应"}
            
        if json_response.get("code") == 200 and "data" in json_response:
            data = json_response.get("data")
            # 修复：增加对 data 是否存在的检查
            if not data:
                return {"error": "新API响应中 data 字段为空"}
            # 新接口返回的是视频链接
            if data.get("url"):
                return {
                    "title": data.get("title", "无标题"),
                    "video_url": data.get("url"),
                    "cover_url": data.get("cover"), # 将 "cover" 映射为 "cover_url"
                    "images": [] # 明确这是一个视频，没有图片
                }
            else:
                return {"error": "新API响应中未找到有效的视频链接"}
        else:
            return {"error": json_response.get("msg", "新API返回未知错误")}

    def _parse_old_api_response(self, json_response: dict) -> Optional[dict]:
        """[新增] 解析旧接口 (api_url) 的返回数据"""
        # 修复：增加对空响应的检查
        if not json_response:
            return {"error": "旧API返回了空响应"}

        if json_response.get("code") == 200 and "data" in json_response:
            # 旧接口返回的数据格式已经是我们需要的标准格式，直接返回即可
            return json_response.get("data")
        else:
            return {"error": json_response.get("msg", "旧API返回未知错误")}

    async def _parse_video_url(self, video_url: str) -> Optional[dict]:
        """
        【优化重构】调用接口解析视频URL。
        1. 优先使用 video_api_url。
        2. 如果失败（网络、超时、API返回错误），则自动切换到 api_url。
        3. 兼容并转换两种API的返回格式。
        """
        if not self.video_api_url and not self.api_url:
            logger.error("未配置任何解析接口URL")
            return {"error": "未配置任何解析接口URL"}

        apis_to_try = []
        if self.video_api_url:
            apis_to_try.append({
                "name": "主接口",
                "base_url": self.video_api_url,
                "parser": self._parse_new_api_response
            })
        if self.api_url:
            apis_to_try.append({
                "name": "备用接口",
                "base_url": self.api_url,
                "parser": self._parse_old_api_response
            })

        if not video_url.startswith(("http://", "https://")):
            video_url = f"https://v.douyin.com/{video_url}"

        last_error = "所有API接口均解析失败"
        
        for api in apis_to_try:
            api_url = f"{api['base_url']}{video_url}"
            logger.info(f"正在尝试使用 {api['name']}: {api['base_url'].split('/api/')[0]}...")
            
            retries = 0
            while retries < self.max_retries:
                try:
                    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'}
                    # 优化：此处使用专用的、更短的 api_timeout
                    async with self.session.get(api_url, headers=headers, timeout=self.api_timeout) as response:
                        if response.status == 200:
                            json_response = await response.json(content_type=None)
                            parsed_data = api['parser'](json_response)
                            
                            if parsed_data and "error" not in parsed_data:
                                logger.success(f"{api['name']} 解析成功！")
                                return parsed_data
                            else:
                                last_error = parsed_data.get("error", "API返回数据格式不正确") if parsed_data else "解析函数返回空"
                                logger.warning(f"{api['name']} 解析失败: {last_error}。")
                                break 
                        else:
                            last_error = f"HTTP状态码: {response.status}"
                            logger.error(f"请求接口失败: {last_error}")
                            if 400 <= response.status < 500:
                                break

                except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                    last_error = f"网络错误: {type(e).__name__}"
                    logger.error(f"接口请求网络异常 (尝试 {retries+1}/{self.max_retries}): {e}")
                except Exception as e:
                    last_error = f"意外错误: {str(e)}"
                    logger.exception(f"调用API过程中发生意外异常，终止重试: {e}")
                    break 
                
                retries += 1
                if retries < self.max_retries:
                    await asyncio.sleep(1)
            
        return {"error": f"解析失败: {last_error}"}

    async def _download_content(self, url: str, content_type: str) -> Optional[bytes]:
        """
        【优化】通用的内容下载函数，集成了视频和图片的下载逻辑。
        通过模拟真实的浏览器请求头来绕过反爬机制。
        """
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'Referer': 'https://www.douyin.com/',
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8' if content_type == 'image' else '*/*',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Connection': 'keep-alive',
        }
        # 视频下载时最好带上Range头，图片则不需要
        if content_type == 'video':
            headers['Range'] = 'bytes=0-'

        retries = 0
        last_error = None
        
        while retries < self.max_retries:
            try:
                async with self.session.get(url, headers=headers, timeout=self.timeout) as response:
                    # 200 (OK) 和 206 (Partial Content) 都表示成功
                    if response.status in [200, 206]:
                        return await response.read()
                    elif response.status == 403:
                        last_error = f"触发反爬机制，不再重试"
                        logger.warning(last_error)
                        break
                    else:
                        last_error = f"下载失败 HTTP {response.status}"
                        logger.error(last_error)
            except Exception as e:
                last_error = f"下载异常(尝试 {retries+1}/{self.max_retries}): {str(e)[:100]}"
                logger.warning(last_error)
            
            retries += 1
            if retries >= self.max_retries:
                break
                
            delay = min(2 ** retries + random.uniform(0, 1), 10)
            await asyncio.sleep(delay)
        
        logger.error(f"{content_type.capitalize()}下载失败，最终错误: {last_error}")
        return None

    async def _download_video(self, video_url: str) -> Optional[bytes]:
        """
        下载视频文件。现在调用通用的下载函数。
        """
        return await self._download_content(video_url, 'video')

    async def _download_image(self, image_url: str) -> Optional[bytes]:
        """
        下载单张图片。现在调用通用的下载函数。
        """
        return await self._download_content(image_url, 'image')

    async def _send_link_card(
        self,
        bot: WechatAPIClient,
        to_wxid: str,
        title: str,
        description: str,
        url: str,
        image_url: str = None
    ):
        """发送链接卡片消息（修复并优化后的版本）"""
        try:
            thumb_url = image_url or ""
            # 使用 html.escape 防止特殊字符破坏XML结构
            safe_title = html.escape(title)
            safe_desc = html.escape(description)
            safe_url = html.escape(url)
            safe_thumb_url = html.escape(thumb_url)

            # 采用能成功显示缩略图的、更完整的XML结构
            xml = f"""<appmsg appid="wx76fdd06dde311af3" sdkver="0">
<title>{safe_title}</title>
<des>{safe_desc}</des>
<action>view</action>
<type>5</type>
<url>{safe_url}</url>
<thumburl>{safe_thumb_url}</thumburl>
<appattach>
<totallen>0</totallen>
<attachid></attachid>
<fileext></fileext>
<tpthumburl>{safe_thumb_url}</tpthumburl>
</appattach>
</appmsg>"""
            
            api_base = f"http://{bot.ip}:{bot.port}"
            api_prefix = "/api"
            
            data = {
                "ToWxid": to_wxid,
                "Type": 5,
                "Wxid": bot.wxid,
                "Xml": xml
            }
            
            # 使用共享的 self.session
            async with self.session.post(
                f"{api_base}{api_prefix}/Msg/SendApp",
                json=data,
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status == 200:
                    resp_data = await response.json()
                    logger.info(f"发送卡片消息成功: {resp_data}")
                    return True
                else:
                    logger.error(f"发送卡片消息失败: HTTP状态码 {response.status}, 响应: {await response.text()}")
                    return False
        except Exception as e:
            logger.exception(f"发送卡片消息时发生异常: {e}")
            return False

    def _sync_process_image(self, image_content: bytes) -> Optional[bytes]:
        """
        [同步函数] 在单独的线程中执行的图片处理核心逻辑。
        """
        try:
            with Image.open(io.BytesIO(image_content)) as img:
                # 统一转换为RGB模式以去除alpha通道（适用于PNG, WEBP等）
                if img.mode in ('RGBA', 'LA', 'P'):
                    # 创建一个白色背景
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    # 粘贴原图，使用alpha通道作为蒙版
                    background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                    img = background
                elif img.mode != 'RGB':
                    img = img.convert('RGB')

                # 将处理后的图片保存到内存中的BytesIO对象
                output_buffer = io.BytesIO()
                img.save(output_buffer, format='JPEG', quality=95)
                jpeg_content = output_buffer.getvalue()

                # 计算最终JPEG内容的MD5
                md5_hash = hashlib.md5(jpeg_content).hexdigest()
                file_name = f"{md5_hash}.jpeg"
                file_path = os.path.join(self.files_dir, file_name)

                # 只有当文件不存在时才写入，避免重复IO
                if not os.path.exists(file_path):
                    with open(file_path, "wb") as f:
                        f.write(jpeg_content)
                    logger.info(f"图片已处理并保存至: {file_path}")
                else:
                    logger.info(f"图片文件已存在，跳过写入: {file_path}")
                
                return jpeg_content
        except Exception as e:
            logger.error(f"同步处理图片时发生错误: {e}")
            logger.error(traceback.format_exc())
            return None

    async def _process_and_save_image(self, image_content: bytes) -> Optional[bytes]:
        """
        [异步函数] 统一处理和保存图片。
        它将CPU密集型的图片处理任务移到单独的线程中执行，防止阻塞主程序。
        """
        if not image_content:
            logger.error("图片内容为空，无法处理。")
            return None
        
        # 使用 asyncio.to_thread 在工作线程中运行同步的、CPU密集型的代码
        return await asyncio.to_thread(self._sync_process_image, image_content)

    def _sync_extract_first_frame(self, video_data: bytes) -> Optional[bytes]:
        """
        [同步函数] 从视频二进制数据中提取第一帧并编码为JPEG。
        使用临时文件来让OpenCV读取内存中的视频数据。
        """
        try:
            # 创建一个带名字的临时文件，OpenCV需要文件路径来读取
            with tempfile.NamedTemporaryFile(delete=True, suffix='.mp4') as temp_video_file:
                temp_video_file.write(video_data)
                temp_video_file.flush() # 确保所有数据都写入文件

                cap = cv2.VideoCapture(temp_video_file.name)
                if not cap.isOpened():
                    logger.error("无法打开视频流或文件")
                    return None
                
                success, frame = cap.read()
                cap.release()

                if success:
                    # 将帧（numpy数组）编码为JPEG格式的二进制数据
                    is_success, buffer = cv2.imencode(".jpg", frame)
                    if is_success:
                        return buffer.tobytes()
                    else:
                        logger.error("帧编码为JPEG失败")
                        return None
                else:
                    logger.error("读取视频第一帧失败")
                    return None
        except Exception as e:
            logger.exception(f"从视频提取帧时发生错误: {e}")
            return None

    async def _extract_first_frame_from_video(self, video_data: bytes) -> Optional[bytes]:
        """
        [异步函数] 提取视频第一帧作为封面。
        将CPU密集型的视频处理任务移到单独的线程中执行。
        """
        if not video_data:
            return None
        return await asyncio.to_thread(self._sync_extract_first_frame, video_data)

    async def _send_images(self, bot: WechatAPIClient, to_wxid: str, images: List[bytes], title: str = "") -> bool:
        """
        发送多张图片到微信(确保所有图片都是JPEG格式)
        
        Args:
            bot: WechatAPIClient实例
            to_wxid: 接收者微信ID
            images: 图片二进制数据列表(已转换为JPEG格式)
            title: 标题文本
            
        Returns:
            bool: 是否发送成功
        """
        try:
            if title:
                await bot.send_text_message(to_wxid, title)
                
            for idx, img_data in enumerate(images, 1):
                try:
                    img_base64 = base64.b64encode(img_data).decode("utf-8")
                    await bot.send_image_message(to_wxid, img_base64)
                    logger.info(f"图片 {idx}/{len(images)} 发送成功")
                    await asyncio.sleep(1)  # 避免发送过快
                except Exception as e:
                    logger.error(f"图片 {idx}/{len(images)} 发送失败: {e}")
                    # 尝试发送失败后，发送链接卡片
                    await self._send_link_card(
                        bot,
                        to_wxid,
                        title or "抖音图片",
                        f"图片 {idx} 发送失败，点击查看原图",
                        "",  # 没有原始URL，留空
                        ""   # 没有封面URL，留空
                    )
            
            return True
        except Exception as e:
            logger.error(f"发送图片失败: {e}")
            return False

    async def _get_sharable_link_info(self, original_url: str) -> Optional[dict]:
        """
        [新增] 专门使用备用接口(api_url)来获取一个可在微信中分享的链接信息。
        """
        if not self.api_url:
            logger.warning("未配置备用接口 (api_url)，无法生成分享卡片链接。")
            return None

        api_url_req = f"{self.api_url}{original_url}"
        logger.info(f"正在使用备用接口获取分享链接: {self.api_url.split('/api/')[0]}...")

        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'}
            async with self.session.get(api_url_req, headers=headers, timeout=self.timeout) as response:
                if response.status == 200:
                    json_response = await response.json(content_type=None)
                    parsed_data = self._parse_old_api_response(json_response)
                    if parsed_data and "error" not in parsed_data:
                        logger.success("成功获取到分享链接信息。")
                        return parsed_data
                    else:
                        error_msg = parsed_data.get("error", "备用接口返回数据格式不正确") if parsed_data else "备用接口返回空"
                        logger.error(f"备用接口未能成功解析: {error_msg}")
                        return None
                else:
                    logger.error(f"请求备用接口失败: HTTP {response.status}")
                    return None
        except Exception as e:
            logger.exception(f"获取分享链接时发生异常: {e}")
            return None

    async def _send_fallback_card(self, bot: WechatAPIClient, chat_id: str, original_url: str, title: str, reason: str, cover_url: Optional[str] = None):
        """
        [新增] 当主流程失败时，获取备用链接并发送卡片。
        """
        logger.info(f"主流程失败 ({reason})，尝试发送备用卡片。")
        
        # 尝试从备用接口获取可分享的链接信息
        sharable_info = await self._get_sharable_link_info(original_url)
        
        # 优先使用备用接口的信息，如果失败则使用主流程的信息或默认值
        card_title = title or (sharable_info.get("title") if sharable_info else "视频分享")
        card_url = sharable_info.get("video_url") if sharable_info and sharable_info.get("video_url") else original_url
        card_cover = sharable_info.get("cover_url") if sharable_info and sharable_info.get("cover_url") else cover_url
        
        # 如果主流程的标题有效，先发送标题
        if title:
            await bot.send_text_message(chat_id, title)

        await self._send_link_card(
            bot,
            chat_id,
            card_title,
            reason,
            card_url,
            card_cover
        )

    async def _handle_image_set(self, bot: WechatAPIClient, chat_id: str, video_info: dict) -> bool:
        """
        处理图片集内容
        """
        try:
            images_urls = video_info.get("images", [])
            if not images_urls:
                await bot.send_text_message(chat_id, "未获取到图片内容")
                return False
                
            title = video_info.get("title", "图片集")
            author_info = video_info.get("author", {})
            if author_info:
                title = f"{title}\n作者: {author_info.get('name', '未知')}"
            
            await bot.send_text_message(chat_id, f"正在下载和处理 {len(images_urls)} 张图片，请稍候...")
            
            # 下载并处理所有图片
            processed_images = []
            for idx, img_url in enumerate(images_urls, 1):
                raw_img_data = await self._download_image(img_url)
                if raw_img_data:
                    # 调用新的处理函数
                    jpeg_data = await self._process_and_save_image(raw_img_data)
                    if jpeg_data:
                        processed_images.append(jpeg_data)
                        logger.info(f"图片 {idx}/{len(images_urls)} 处理成功")
                    else:
                        logger.warning(f"图片 {idx}/{len(images_urls)} 处理失败")
                else:
                    logger.warning(f"图片 {idx}/{len(images_urls)} 下载失败")
            
            if not processed_images:
                await bot.send_text_message(chat_id, "所有图片处理失败，请稍后重试")
                return False
                
            # 发送图片
            success = await self._send_images(bot, chat_id, processed_images, title)
            if not success:
                await bot.send_text_message(chat_id, "图片发送失败，尝试发送链接...")
                await self._send_link_card(
                    bot,
                    chat_id,
                    title,
                    f"共 {len(images_urls)} 张图片，点击查看",
                    images_urls[0],
                    images_urls[0]
                )
            
            return success
        except Exception as e:
            logger.exception(f"处理图片集失败: {e}")
            await bot.send_text_message(chat_id, f"处理图片集失败: {str(e)[:100]}")
            return False

    @on_text_message
    async def handle_text_message(self, bot: WechatAPIClient, message: dict):
        """处理文本消息（优化版）"""
        if not self.enable:
            return True
        
        content = message["Content"].strip()
        chat_id = message["FromWxid"]
    
        original_url = self._extract_video_url(content)
        
        if not original_url:
            return True
        
        try:
            #await bot.send_text_message(chat_id, "正在解析链接内容，请稍候...")
            video_info = await self._parse_video_url(original_url)
            
            # 场景一：主备接口全部解析失败
            if not video_info or "error" in video_info:
                error_msg = video_info.get("error", "解析链接内容失败") if isinstance(video_info, dict) else "解析链接内容失败"
                # 即使主流程解析失败，也尝试用备用接口生成卡片
                await self._send_fallback_card(bot, chat_id, original_url, "视频分享", f"解析失败: {error_msg}")
                return False
    
            if video_info.get("images"):
                return not await self._handle_image_set(bot, chat_id, video_info)
            
            elif video_info.get("video_url"):
                download_url = video_info["video_url"]
                title = video_info.get("title", "无标题")
                cover_url = video_info.get("cover_url")

                # 检查视频大小
                try:
                    async with self.session.head(download_url, timeout=10, allow_redirects=True) as head_response:
                        if head_response.status == 200 and 'Content-Length' in head_response.headers:
                            video_size = int(head_response.headers['Content-Length'])
                            # 场景二：视频过大
                            if video_size > self.max_video_size * 1024 * 1024:
                                reason = f"视频过大({video_size / 1024 / 1024:.2f}MB)\n点击查看完整视频"
                                await self._send_fallback_card(bot, chat_id, original_url, title, reason, cover_url)
                                return False
                except Exception as e:
                    logger.warning(f"检查视频大小失败: {e}，将继续尝试下载。")

                video_data = await self._download_video(download_url)
                
                # 场景三：视频下载失败
                if not video_data:
                    await self._send_fallback_card(bot, chat_id, original_url, title, "视频下载失败，点击查看", cover_url)
                    return False
                    
                await bot.send_text_message(chat_id, f"{title}")
                
                try:
                    video_base64 = base64.b64encode(video_data).decode("utf-8")
                    
                    cover_data = await self._download_image(cover_url) if cover_url else None
                    
                    if not cover_data and video_data:
                        logger.info("封面图下载失败，尝试从视频第一帧提取...")
                        cover_data = await self._extract_first_frame_from_video(video_data)
                        if cover_data:
                            logger.info("成功从视频第一帧提取封面图。")
                        else:
                            logger.warning("从视频第一帧提取封面图失败。")

                    cover_base64 = base64.b64encode(cover_data).decode("utf-8") if cover_data else None

                    await asyncio.wait_for(
                        bot.send_video_message(chat_id, video=video_base64, image=cover_base64),
                        timeout=120
                    )
                except Exception as send_error:
                    # 场景四：视频文件发送失败
                    logger.error(f"视频发送失败，转为卡片: {send_error}")
                    await self._send_fallback_card(bot, chat_id, original_url, title, "视频发送失败，点击查看", cover_url)
            else:
                await bot.send_text_message(chat_id, "未识别到有效的视频或图片内容")
                return False
                
        except Exception as e:
            logger.exception(f"处理异常: {e}")
            await bot.send_text_message(chat_id, "哎呀，处理时遇到一点小问题，请稍后再试或联系管理员。")
        
        return False

    async def close(self):
        """插件关闭时执行的操作，关闭网络会话。"""
        if self.session and not self.session.closed:
            await self.session.close()
        logger.info("MediaParser 插件已关闭，网络会话已清理。")

