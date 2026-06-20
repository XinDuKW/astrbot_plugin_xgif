import os
import asyncio
import aiohttp
import uuid
import re
import logging
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig

logger = logging.getLogger("xgif")

@register("xgif","XinDuKW", "发送推文链接或引用推文消息，通过FFmpeg将X (Twitter)动图转为常规 GIF 格式表情包发送","1.1.0")

class TwitterGifConverter(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        proxy_url = self.config.get("proxy_url", "")
        self.proxy_url = proxy_url.strip() if proxy_url else None
        self.temp_dir = "./temp_gif"
        os.makedirs(self.temp_dir, exist_ok=True)




    async def safe_send(self, event, text):
        try:
            group_id = event.get_group_id()
            user_id = event.get_sender_id()
            msg_type = "group" if group_id else "private"
            await event.bot.send_msg(message_type=msg_type, user_id=user_id, group_id=group_id, message=text)
        except Exception as e:
            logger.error(f"[TwitterGif] 发送消息失败: {e}")

    def extract_tweet_url(self, text):
        if not text: return None
        match = re.search(r'(https?://(?:x|twitter)\.com/\S+/status/\S+)', text)
        return match.group(1) if match else None

    async def process_gif_conversion(self, event, tweet_url):
        await self.safe_send(event, "🎬 正在解析并下载视频...")
        real_video_url = None
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        
        logger.info(f"[TwitterGif] 当前使用代理: {self.proxy_url}")

        try:

            async with aiohttp.ClientSession() as session:
                async with session.get(tweet_url, headers=headers, proxy=self.proxy_url) as resp:
                    if resp.status == 200:
                        html_content = await resp.text()
                        video_match = re.search(r'(https?://video\.twimg\.com/[^\s"\'<>]+\.(?:mp4|m3u8)[^\s"\'<>]*)', html_content)
                        if video_match: real_video_url = video_match.group(1)
        except Exception as e:
            await self.safe_send(event, f"❌ 解析网络错误: {str(e)}")
            return

        if not real_video_url:
            await self.safe_send(event, "❌ 无法提取视频源地址。")
            return

        local_mp4 = os.path.join(self.temp_dir, f"{uuid.uuid4().hex}.mp4")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(real_video_url, headers=headers, proxy=self.proxy_url) as resp:
                    if resp.status == 200:
                        with open(local_mp4, 'wb') as f: f.write(await resp.read())
                    else:
                        await self.safe_send(event, "❌ 视频下载失败。")
                        return
        except Exception as e:
            await self.safe_send(event, f"❌ 下载错误: {str(e)}")
            return

        await self.safe_send(event, "🎞️ 正在转换为 GIF...")
        local_gif = local_mp4.replace(".mp4", ".gif")
        
        cmd = [
            'ffmpeg', '-i', local_mp4, 
            '-vf', 'fps=10,scale=320:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse', 
            '-loop', '0', 
            '-y', 
            local_gif
        ]

        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await proc.communicate()
            
            if proc.returncode == 0 and os.path.exists(local_gif):
                abs_gif_path = os.path.abspath(local_gif)
                group_id = event.get_group_id()
                user_id = event.get_sender_id()
                msg_type = "group" if group_id else "private"
                file_msg = [{"type": "image", "data": {"file": f"file:///{abs_gif_path}"}}]
                await event.bot.send_msg(message_type=msg_type, user_id=user_id, group_id=group_id, message=file_msg)
            else:
                error_msg = stderr.decode('utf-8', errors='ignore')
                logger.error(f"[TwitterGif] ❌ FFmpeg 转换失败，详细错误:\n{error_msg}")
                await self.safe_send(event, "❌ FFmpeg 转换失败，请查看控制台日志。")
                
        except Exception as e: 
            await self.safe_send(event, f"❌ 转换错误: {str(e)}")
        finally:
            if os.path.exists(local_mp4): os.remove(local_mp4)
            if os.path.exists(local_gif): os.remove(local_gif)

    @filter.command("转gif")
    async def convert_gif(self, event: AstrMessageEvent, link: str = ""):
        tweet_url = None
        
        message_chain = getattr(event.message_obj, "message", [])

        # 1. 优先检查指令参数
        if link and link.startswith("http"):
            tweet_url = link
        
        # 2. 检查当前消息文本
        if not tweet_url:
            message_chain = getattr(event.message_obj, "message", [])
            if isinstance(message_chain, list):
                for seg in message_chain:
                    if getattr(seg, "type", None) == "plain":
                        text = getattr(seg, "text", "")
                        tweet_url = self.extract_tweet_url(text)
                        if tweet_url: break

        # 3. WebSocket 调用 OneBot 的 get_msg 接口
        if not tweet_url and isinstance(message_chain, list):
            for seg in message_chain:
                if getattr(seg, "type", None) == "Reply":
                    reply_id = getattr(seg, "id", None)
                    if reply_id:
                        logger.info(f"[TwitterGif] 🎯 发现引用消息 ID: {reply_id}，正在通过 WebSocket 拉取...")
                        try:
                            quoted_msg = await event.bot.get_msg(message_id=int(reply_id))
                            raw_message = quoted_msg.get("message", [])
                            if isinstance(raw_message, list):
                                for q_seg in raw_message:
                                    if q_seg.get("type") == "text":
                                        q_text = q_seg.get("data", {}).get("text", "")
                                        tweet_url = self.extract_tweet_url(q_text)
                                        if tweet_url:
                                            logger.info(f"[TwitterGif] ✅ 成功提取到链接: {tweet_url}")
                                            break
                        except Exception as e:
                            logger.error(f"[TwitterGif] ❌ 调用 get_msg 失败: {e}")
                    if tweet_url: break

        if not tweet_url:
            await self.safe_send(event, "❌ 未检测到推特链接！\n\n✅ 正确用法：\n1. /转gif <推特链接>\n2. 引用推文消息后发送 /转gif")
            return

        await self.process_gif_conversion(event, tweet_url)