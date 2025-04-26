from typing import TYPE_CHECKING

from nonebot.adapters import Bot, Event
from nonebot.adapters.feishu.event import MessageEvent
from nonebot.adapters.feishu.message import At as AtSegment
from nonebot.adapters.feishu.message import File as FileSegment
from nonebot.adapters.feishu.message import Post as PostSegment
from nonebot.adapters.feishu.message import Audio as AudioSegment
from nonebot.adapters.feishu.message import Image as ImageSegment
from nonebot.adapters.feishu.message import Media as MediaSegment
from nonebot.adapters.feishu.models import UserId as FeishuUserId
from nonebot.adapters.feishu.message import Folder as FolderSegment
from nonebot.adapters.feishu.message import Message, MessageSegment
from nonebot.adapters.feishu.models import Mention as FeishuMention

from nonebot_plugin_alconna.uniseg.constraint import SupportAdapter
from nonebot_plugin_alconna.uniseg.builder import MessageBuilder, build
from nonebot_plugin_alconna.uniseg.segment import At, File, Text, AtAll, Audio, Emoji, Image, Reply, Video


class FeishuMessageBuilder(MessageBuilder):
    @classmethod
    def get_adapter(cls) -> SupportAdapter:
        return SupportAdapter.feishu

    @build("at")
    def at(self, seg: AtSegment):
        if seg.data["user_id"] in ("all", "here"):
            return AtAll(here=seg.data["user_id"] == "here")
        return At("user", str(seg.data["user_id"]))

    @build("image", "img")  # img for PostImg
    def image(self, seg: ImageSegment):
        return Image(id=seg.data["image_key"])

    @build("media")
    def media(self, seg: MediaSegment):
        return Video(id=seg.data["file_key"], name=seg.data.get("file_name") or "video.mp4")

    @build("audio")
    def audio(self, seg: AudioSegment):
        return Audio(url=seg.data["file_key"])

    @build("file")
    def file(self, seg: FileSegment):
        return File(
            id=seg.data["file_key"],
            name=seg.data.get("file_name") or seg.data["file_key"],
        )

    @build("folder")
    def folder(self, seg: FolderSegment):
        return File(
            id=seg.data["file_key"],
            name=seg.data.get("file_name") or seg.data["file_key"],
        )

    @build("a")  # PostA
    def a(self, seg: MessageSegment):
        return Text(text=seg.data["text"]).link()

    @build("emotion")  # PostEmotion
    def emotion(self, seg: MessageSegment):
        return Emoji(id=seg.data["emoji_type"])

    @build("post")
    def post(self, seg: PostSegment):
        result = []
        # ref: https://github.com/nonebot/adapter-feishu/blob/v2.6.2/nonebot/adapters/feishu/message.py#L428
        for line in seg.data["content"]:
            for node in line:  # should be PostMessageNode
                node = node.copy()
                converted = self.convert(MessageSegment(node.pop("tag"), node))
                result.extend(converted if isinstance(converted, list) else [converted])
        return result

    async def extract_reply(self, event: Event, bot: Bot):
        if TYPE_CHECKING:
            assert isinstance(event, MessageEvent)
        if event.reply:
            # ReplyMention is incompatible with Mention
            # but Message.deserialize only uses Mention.id.open_id
            # ref: https://github.com/nonebot/adapter-feishu/blob/v2.6.2/nonebot/adapters/feishu/message.py#L642
            mentions = [
                FeishuMention(
                    key=m.key,
                    id=FeishuUserId(union_id="", user_id="", open_id=(m.id if m.id_type == "open_id" else "")),
                    name=m.name,
                    tenant_key=m.tenant_key,
                )
                for m in event.reply.mentions
            ]
            msg = Message.deserialize(
                content=event.reply.body.content,
                mentions=mentions,
                message_type=event.reply.msg_type,
            )
            return Reply(event.reply.message_id, msg, event.reply)
        return None
