from arclet.alconna import Args, Alconna


def test_kook():
    from nonebot.adapters.kaiheila.message import Message, MessageSegment

    from nonebot_plugin_alconna.adapters.kook import At, KMarkdown

    msg = Message([MessageSegment.text("/command "), MessageSegment.KMarkdown("(met)123456(met)12345678")])

    alc = Alconna("/command", Args["some_arg", At]["some_arg1", KMarkdown])

    res = alc.parse(msg)
    assert res.matched
    assert res.some_arg.type == "at"
    assert res.some_arg.data["user_id"] == "123456"
    assert res.some_arg1.data["content"] == "12345678"
