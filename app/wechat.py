"""微信公众号接入：签名校验 + 消息解析 + 回复生成。

架构：
  用户在微信发消息 → 微信服务器 → 本项目的 /wechat 接口
    → 复用 router.process_ticket() 处理 → 返回 XML 文本回复给微信

参考文档（微信公众平台）：
  https://developers.weixin.qq.com/doc/offiaccount/Message_Management/Receiving_standard_messages.html
"""
import hashlib
import time
import xml.etree.ElementTree as ET

from app import config


def verify_signature(signature: str, timestamp: str, nonce: str) -> bool:
    """校验微信服务器传来的签名（首次配置「服务器 URL」时验证用）。

    微信算法：对 [token, timestamp, nonce] 按字典序排序后拼接，做 sha1。
    """
    token = config.Config.WECHAT_TOKEN
    raw = "".join(sorted([token, timestamp, nonce]))
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return digest == signature


def parse_message(xml_text: bytes | str) -> dict:
    """解析微信发来的 XML 消息，返回 {标签: 文本} 字典。"""
    if isinstance(xml_text, bytes):
        xml_text = xml_text.decode("utf-8")
    root = ET.fromstring(xml_text)
    return {child.tag: (child.text or "") for child in root}


def build_text_reply(to_user: str, from_user: str, content: str) -> str:
    """生成微信文本回复 XML。

    注意：收发双方的 To/From 正好对调。
      - 收到消息里 FromUserName = 用户 openid，ToUserName = 公众号 id
      - 回复消息里要反过来。
    """
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        "</xml>"
    )
