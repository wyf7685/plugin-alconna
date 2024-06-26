from __future__ import annotations

import random
import weakref
from warnings import warn
from types import FunctionType
from datetime import datetime, timedelta
from collections.abc import Hashable, Iterable
from typing_extensions import Self, deprecated
from typing import TYPE_CHECKING, Any, Union, Literal, TypeVar, Callable, ClassVar, NoReturn, Protocol, cast, overload

from nonebot.rule import Rule
from nonebot.params import Depends
from nonebot.utils import escape_tag
from tarina.lang.model import LangItem
from nonebot import require, get_driver
from nonebot.permission import Permission
from nonebot.dependencies import Dependent
from nonebot.message import run_postprocessor
from nonebot.consts import ARG_KEY, RECEIVE_KEY
from nonebot.internal.params import DefaultParam
from tarina import is_awaitable, run_always_await
from arclet.alconna.typing import ShortcutRegWrapper
from nepattern import ANY, STRING, TPattern, AnyString
from _weakref import _remove_dead_weakref  # type: ignore
from arclet.alconna.tools import AlconnaFormat, AlconnaString
from nonebot.plugin.on import store_matcher, get_matcher_source
from arclet.alconna.tools.construct import FuncMounter, MountConfig
from arclet.alconna import Arg, Args, Alconna, ShortcutArgs, command_manager
from nonebot.exception import PausedException, FinishedException, RejectedException
from nonebot.internal.adapter import Bot, Event, Message, MessageSegment, MessageTemplate
from nonebot.matcher import Matcher, matchers, current_bot, current_event, current_matcher
from nonebot.typing import T_State, T_Handler, T_RuleChecker, T_PermissionChecker, _DependentCallable

from .i18n import Lang
from .rule import alconna
from .typings import MReturn
from .util import annotation
from .model import CompConfig
from .pattern import patterns
from .uniseg import Text, Segment, UniMessage
from .uniseg.fallback import FallbackStrategy
from .uniseg.template import UniMessageTemplate
from .extension import Extension, ExtensionExecutor
from .consts import ALCONNA_RESULT, ALCONNA_ARG_KEY, log
from .params import (
    CHECK,
    MIDDLEWARE,
    Check,
    AlconnaParam,
    AlcExecResult,
    ExtensionParam,
    assign,
    _seminal,
    _Dispatch,
    merge_path,
)

_M = Union[str, Message, MessageSegment, MessageTemplate, Segment, UniMessage, UniMessageTemplate]


class ArgsMounter(Protocol):
    args: Args


def extract_arg(path: str, target: ArgsMounter | None) -> Arg | None:
    """从 Alconna 中提取参数"""
    if not target:
        return
    parts = path.split(".")
    if len(parts) == 1:
        return next((arg for arg in target.args.argument if arg.name == path), None)
    _parts, end = parts[:-1], parts[-1]
    if not (options := getattr(target, "options", None)):
        return
    for opt in options:
        if opt.dest == _parts[0]:
            return extract_arg(".".join(_parts[1:] + [end]), opt)
    return


def _validate(target: Arg[Any], arg: Segment):
    value = target.value
    if value == ANY:
        return arg
    if value == AnyString or (value == STRING and arg.is_text()):
        return str(arg)
    default_val = target.field.default
    if arg.is_text():
        arg = arg.data["text"]
    res = value.validate(arg, default_val)
    if target.optional and res.flag != "valid":
        return
    if res.flag == "error":
        return
    return res._value  # noqa


class _method:
    def __init__(self, func: FunctionType):
        self.__func__ = func

    def __get__(self, instance, owner):
        if instance is None:
            return self.__func__.__get__(owner, owner)
        return self.__func__.__get__(instance, owner)


R = TypeVar("R")
R1 = TypeVar("R1")
T = TypeVar("T")
T1 = TypeVar("T1")


class AlconnaMatcher(Matcher):
    command: ClassVar[weakref.ReferenceType[Alconna]]
    basepath: ClassVar[str]
    executor: ClassVar[ExtensionExecutor]
    _command_path: ClassVar[str]
    _tests: ClassVar[list[tuple[UniMessage, dict[str, Any] | None, bool]]]

    @classmethod
    @overload
    def shortcut(cls, key: str | TPattern, args: ShortcutArgs | None = None) -> type[Self]:
        """操作快捷命令

        Args:
            key (str | re.Pattern[str]): 快捷命令名, 可传入正则表达式
            args (ShortcutArgs[TDC]): 快捷命令参数, 不传入时则尝试使用最近一次使用的命令

        Returns:
            str: 操作结果

        Raises:
            ValueError: 快捷命令操作失败时抛出
        """
        ...

    @classmethod
    @overload
    def shortcut(
        cls,
        key: str | TPattern,
        *,
        command: str | None = None,
        arguments: list[Any] | None = None,
        fuzzy: bool = True,
        prefix: bool = False,
        wrapper: ShortcutRegWrapper | None = None,
        humanized: str | None = None,
    ) -> type[Self]:
        """操作快捷命令

        Args:
            key (str | re.Pattern[str]): 快捷命令名, 可传入正则表达式
            command (TDC): 快捷命令指向的命令
            arguments (list[Any] | None, optional): 快捷命令参数, 默认为 `None`
            fuzzy (bool, optional): 是否允许命令后随参数, 默认为 `True`
            prefix (bool, optional): 是否调用时保留指令前缀, 默认为 `False`
            wrapper (ShortcutRegWrapper, optional): 快捷指令的正则匹配结果的额外处理函数, 默认为 `None`
            humanized (str, optional): 快捷指令的人类可读描述, 默认为 `None`

        Returns:
            str: 操作结果

        Raises:
            ValueError: 快捷命令操作失败时抛出
        """
        ...

    @classmethod
    def shortcut(cls, key: str | TPattern, args: ShortcutArgs | None = None, **kwargs):
        """操作快捷命令

        Args:
            key (str | re.Pattern[str]): 快捷命令名, 可传入正则表达式
            args (ShortcutArgs[TDC] | None, optional): 快捷命令参数, 不传入时则尝试使用最近一次使用的命令
            command (TDC, optional): 快捷命令指向的命令
            arguments (list[Any] | None, optional): 快捷命令参数, 默认为 `None`
            fuzzy (bool, optional): 是否允许命令后随参数, 默认为 `True`
            prefix (bool, optional): 是否调用时保留指令前缀, 默认为 `False`
            wrapper (ShortcutRegWrapper, optional): 快捷指令的正则匹配结果的额外处理函数, 默认为 `None`
            humanized (str, optional): 快捷指令的人类可读描述, 默认为 `None`

        Returns:
            str: 操作结果

        Raises:
            ValueError: 快捷命令操作失败时抛出
        """
        cls.command().shortcut(key, args, **kwargs)  # type: ignore
        return cls

    if TYPE_CHECKING:

        @classmethod
        def set_path_arg(cls_or_self, path: str, content: Any) -> None:  # type: ignore
            """设置一个 `got_path` 内容

            Args:
                path (str): 路径; "~XX" 时会把 "~" 替换为父级 assign 的 path
                content (Any): 内容
            """

        @classmethod
        def get_path_arg(cls_or_self, path: str, default: Any) -> Any:  # type: ignore
            """获取一个 `got_path` 内容

            Args:
                path (str): 路径; "~XX" 时会把 "~" 替换为父级 assign 的 path
                default (Any): 默认值
            """

    else:

        @_method
        def set_path_arg(cls_or_self, path: str, content: Any) -> None:
            """设置一个 `got_path` 内容"""
            if isinstance(cls_or_self, AlconnaMatcher):
                state = cls_or_self.state
            else:
                state = current_matcher.get().state
            state[ALCONNA_ARG_KEY.format(key=merge_path(path, cls_or_self.basepath))] = content

        @_method
        def get_path_arg(cls_or_self, path: str, default: Any) -> Any:
            """获取一个 `got_path` 内容"""
            if isinstance(cls_or_self, AlconnaMatcher):
                state = cls_or_self.state
            else:
                state = current_matcher.get().state
            return state.get(ALCONNA_ARG_KEY.format(key=merge_path(path, cls_or_self.basepath)), default)

    @classmethod
    def assign(
        cls,
        path: str,
        value: Any = _seminal,
        or_not: bool = False,
        additional: CHECK | None = None,
        parameterless: Iterable[Any] | None = None,
    ) -> Callable[[T_Handler], T_Handler]:
        """装饰一个函数来向事件响应器直接添加一个处理函数

        此装饰是 @handle([Check(assign(...))]) 的快捷写法

        参数:
            path: 指定的查询路径;
                "$main" 表示没有任何选项/子命令匹配的时候;
                "~XX" 时会把 "~" 替换为父级 assign 的 path
            value: 可能的指定查询值
            or_not: 是否同时处理没有查询成功的情况
            additional: 额外的检查函数
            parameterless: 非参数类型依赖列表
        """
        parameterless = [
            Check(assign(merge_path(path, cls.basepath), value, or_not, additional)),
            *(parameterless or []),
        ]

        def _decorator(func: T_Handler) -> T_Handler:
            cls.append_handler(func, parameterless=parameterless)
            return func

        return _decorator

    @classmethod
    def dispatch(
        cls,
        path: str,
        value: Any = _seminal,
        or_not: bool = False,
        additional: CHECK | None = None,
        rule: Rule | T_RuleChecker | None = None,
        permission: Permission | T_PermissionChecker | None = None,
        *,
        handlers: list[T_Handler | Dependent] | None = None,
        temp: bool = False,
        expire_time: datetime | timedelta | None = None,
        priority: int = 1,
        block: bool = False,
        state: T_State | None = None,
        _depth: int = 0,
    ) -> type[AlconnaMatcher]:
        """注册一个消息事件响应器，
        并且当消息由指定 Alconna 解析并且结果符合 assign 预期时执行

        参数:
            path: 指定的查询路径;
                "$main" 表示没有任何选项/子命令匹配的时候;
                "~XX" 时会把 "~" 替换为父级 assign 的 path
            value: 可能的指定查询值
            or_not: 是否同时处理没有查询成功的情况
            additional: 额外的检查函数
            rule: 事件响应规则
            permission: 事件响应权限
            handlers: 事件处理函数列表
            temp: 是否为临时事件响应器（仅执行一次）
            expire_time: 事件响应器最终有效时间点，过时即被删除
            priority: 事件响应器优先级，其会基于父级事件响应器的优先级进行累加
            block: 是否阻止事件向更低优先级传递
            state: 默认 state
        """

        fn = _Dispatch(merge_path(path, cls.basepath), value, or_not, additional)
        cls.handle()(fn.set)

        matcher: type[AlconnaMatcher] = cls.new(
            "",
            rule & Rule(fn),
            Permission() | permission,
            temp=temp,
            expire_time=expire_time,
            priority=cls.priority + priority,
            block=block,
            handlers=handlers,
            source=get_matcher_source(_depth + 1),
            default_state=state,
        )
        store_matcher(matcher)
        matcher.command = cls.command
        matcher.basepath = merge_path(path, cls.basepath)
        matcher.executor = cls.executor
        return matcher

    @classmethod
    def handle(
        cls,
        parameterless: Iterable[Any] | None = None,
        override: tuple[Literal["insert", "replace"], int] | None = None,
    ) -> Callable[[T_Handler], T_Handler]:
        """装饰一个函数来向事件响应器直接添加一个处理函数

        参数:
            parameterless: 非参数类型依赖列表
            override: 是否定制优先级
        """

        def _decorator(func: T_Handler) -> T_Handler:
            if override is None:
                cls.append_handler(func, parameterless=parameterless)
            else:
                handler_ = Dependent[Any].parse(
                    call=func,
                    parameterless=parameterless,
                    allow_types=cls.HANDLER_PARAM_TYPES,
                )
                if override[0] == "insert":
                    cls.handlers.insert(override[1], handler_)
                else:
                    cls.handlers[override[1]] = handler_
            return func

        return _decorator

    @classmethod
    def receive(
        cls,
        id: str = "",
        parameterless: Iterable[Any] | None = None,
        override: tuple[Literal["insert", "replace"], int] | None = None,
    ) -> Callable[[T_Handler], T_Handler]:
        """装饰一个函数来指示 NoneBot 在接收用户新的一条消息后继续运行该函数

        参数:
            id: 消息 ID，若不指定则使用当前消息 ID
            parameterless: 非参数类型依赖列表
            override: 是否定制优先级
        """

        async def _receive(event: Event, matcher: Matcher) -> None | NoReturn:
            nonlocal id
            if not id:
                try:
                    id = UniMessage.get_message_id(event, current_bot.get())
                except Exception:
                    pass
            matcher.set_target(RECEIVE_KEY.format(id=id))
            if matcher.get_target() == RECEIVE_KEY.format(id=id):
                matcher.set_receive(id, event)
                return
            if matcher.get_receive(id, ...) is not ...:
                return
            await matcher.reject()

        _parameterless = (Depends(_receive), *(parameterless or ()))

        def _decorator(func: T_Handler) -> T_Handler:
            if cls.handlers and cls.handlers[-1].call is func:
                func_handler = cls.handlers[-1]
                new_handler = Dependent(
                    call=func_handler.call,
                    params=func_handler.params,
                    parameterless=Dependent.parse_parameterless(tuple(_parameterless), cls.HANDLER_PARAM_TYPES)
                    + func_handler.parameterless,
                )
                cls.handlers[-1] = new_handler
            elif override is None:
                cls.append_handler(func, parameterless=_parameterless)
            else:
                handler_ = Dependent[Any].parse(
                    call=func,
                    parameterless=_parameterless,
                    allow_types=cls.HANDLER_PARAM_TYPES,
                )
                mode, index = override
                if mode == "insert":
                    cls.handlers.insert(index, handler_)
                else:
                    cls.handlers[index] = handler_

            return func

        return _decorator

    @classmethod
    def got(
        cls,
        key: str,
        prompt: _M | None = None,
        parameterless: Iterable[Any] | None = None,
        fallback: bool | FallbackStrategy = FallbackStrategy.ignore,
        override: tuple[Literal["insert", "replace"], int] | None = None,
    ) -> Callable[[T_Handler], T_Handler]:
        """装饰一个函数来指示 NoneBot 获取一个参数 `key`

        当要获取的 `key` 不存在时接收用户新的一条消息再运行该函数，
        如果 `key` 已存在则直接继续运行

        参数:
            key: 参数名
            prompt: 在参数不存在时向用户发送的消息, 支持 `UniMessage`
            parameterless: 非参数类型依赖列表
            fallback: 若 UniMessage 中的元素无法被解析为当前 adapter 的消息元素时的策略
            override: 是否定制优先级
        """

        @annotation(event=Event, matcher=AlconnaMatcher, bot=Bot, state=T_State)
        async def _key_getter(event: Event, matcher: AlconnaMatcher, bot: Bot, state: T_State):
            matcher.set_target(ARG_KEY.format(key=key))
            if matcher.get_target() == ARG_KEY.format(key=key):
                msg = await cls.executor.select(bot, event).message_provider(event, state, bot)
                if not msg:
                    await matcher.reject(prompt, fallback=fallback)
                if isinstance(msg, UniMessage):
                    msg = await msg.export(bot, True)
                matcher.set_arg(key, msg)
                return
            if matcher.get_arg(key, ...) is not ...:
                return
            await matcher.reject(prompt, fallback=fallback)

        _parameterless = (Depends(_key_getter), *(parameterless or ()))

        def _decorator(func: T_Handler) -> T_Handler:
            if cls.handlers and cls.handlers[-1].call is func:
                func_handler = cls.handlers[-1]
                new_handler = Dependent(
                    call=func_handler.call,
                    params=func_handler.params,
                    parameterless=Dependent.parse_parameterless(tuple(_parameterless), cls.HANDLER_PARAM_TYPES)
                    + func_handler.parameterless,
                )
                cls.handlers[-1] = new_handler
            elif override is None:
                cls.append_handler(func, parameterless=_parameterless)
            else:
                handler_ = Dependent[Any].parse(
                    call=func,
                    parameterless=_parameterless,
                    allow_types=cls.HANDLER_PARAM_TYPES,
                )
                mode, index = override
                if mode == "insert":
                    cls.handlers.insert(index, handler_)
                else:
                    cls.handlers[index] = handler_
            return func

        return _decorator

    @classmethod
    def got_path(
        cls,
        path: str,
        prompt: _M | None = None,
        middleware: MIDDLEWARE | None = None,
        parameterless: Iterable[Any] | None = None,
        fallback: bool | FallbackStrategy = FallbackStrategy.ignore,
        override: tuple[Literal["insert", "replace"], int] | None = None,
    ) -> Callable[[T_Handler], T_Handler]:
        """装饰一个函数来指示 NoneBot 获取一个路径下的参数 `path`

        当要获取的 `path` 不存在时接收用户新的一条消息再运行该函数

        如果 `path` 已存在则直接继续运行

        本插件会获取消息的最后一个消息段并转为 path 对应的类型

        参数:
            path: 参数路径名; "~XX" 时会把 "~" 替换为父级 assign 的 path
            prompt: 在参数不存在时向用户发送的消息，支持 `UniMessage`
            parameterless: 非参数类型依赖列表
            fallback: 若 UniMessage 中的元素无法被解析为当前 adapter 的消息元素时的策略
            override: 是否定制优先级
        """
        path = merge_path(path, cls.basepath)
        if not (arg := extract_arg(path, cls.command())):
            raise ValueError(Lang.nbp_alc.error.matcher_got_path(path=path, cmd=cls.command))

        @annotation(event=Event, bot=Bot, matcher=AlconnaMatcher, state=T_State)
        async def _key_getter(event: Event, bot: Bot, matcher: AlconnaMatcher, state: T_State):
            matcher.set_target(ALCONNA_ARG_KEY.format(key=path))
            if matcher.get_target() == ALCONNA_ARG_KEY.format(key=path):
                msg = await cls.executor.select(bot, event).message_provider(event, state, bot)
                if not msg:
                    await matcher.reject(prompt, fallback=fallback)
                if not isinstance(msg, UniMessage):
                    msg = await UniMessage.generate(message=msg, event=event, bot=bot)
                ms = msg[0]
                if isinstance(ms, Text) and not ms.text.strip():
                    await matcher.reject(prompt, fallback=fallback)
                log("DEBUG", escape_tag(Lang.nbp_alc.log.got_path.ms(path=path, ms=ms)))
                if (res := _validate(arg, ms)) is None:  # type: ignore
                    log(
                        "TRACE",
                        escape_tag(Lang.nbp_alc.log.got_path.validate(path=path, validate=res)),
                    )
                    await matcher.reject(prompt, fallback=fallback)
                if middleware:
                    res = await run_always_await(middleware, event, bot, matcher.state, res)
                matcher.set_path_arg(path, res)
                return
            if matcher.state.get(ALCONNA_ARG_KEY.format(key=path), ...) is not ...:
                return
            await matcher.reject(prompt, fallback=fallback)

        _parameterless = (*(parameterless or ()), Depends(_key_getter))

        def _decorator(func: T_Handler) -> T_Handler:
            if cls.handlers and cls.handlers[-1].call is func:
                func_handler = cls.handlers[-1]
                new_handler = Dependent(
                    call=func_handler.call,
                    params=func_handler.params,
                    parameterless=Dependent.parse_parameterless(tuple(_parameterless), cls.HANDLER_PARAM_TYPES)
                    + func_handler.parameterless,
                )
                cls.handlers[-1] = new_handler
            elif override is None:
                cls.append_handler(func, parameterless=_parameterless)
            else:
                handler_ = Dependent[Any].parse(
                    call=func,
                    parameterless=_parameterless,
                    allow_types=cls.HANDLER_PARAM_TYPES,
                )
                mode, index = override
                if mode == "insert":
                    cls.handlers.insert(index, handler_)
                else:
                    cls.handlers[index] = handler_

            return func

        return _decorator

    @classmethod
    def convert(cls, message: _M) -> Message | UniMessage | str:
        bot = current_bot.get()
        event = current_event.get()
        state = current_matcher.get().state
        if isinstance(message, MessageTemplate):
            return message.format(**state[ALCONNA_RESULT].result.all_matched_args, **state)
        if isinstance(message, UniMessageTemplate):
            extra = {"$event": event, "$target": UniMessage.get_target(event, bot)}
            try:
                msg_id = UniMessage.get_message_id(event, bot)
            except Exception:
                pass
            else:
                extra["$message_id"] = msg_id
            return message.format(**state[ALCONNA_RESULT].result.all_matched_args, **state, **extra)
        if isinstance(message, Segment):
            return UniMessage(message)
        if isinstance(message, MessageSegment):
            return message.get_message_class()(message)
        return message

    @classmethod
    def i18n(
        cls,
        item_or_scope: LangItem | str,
        type_: str | None = None,
        /,
        *args,
        mapping: dict | None = None,
        **kwargs,
    ) -> UniMessage:
        bot = current_bot.get()
        event = current_event.get()
        state = current_matcher.get().state
        msg = UniMessage.i18n(item_or_scope, type_, *args, mapping=mapping, **kwargs)
        extra = {
            **state[ALCONNA_RESULT].result.all_matched_args,
            **state,
            "$event": event,
            "$target": UniMessage.get_target(event, bot),
        }
        try:
            msg_id = UniMessage.get_message_id(event, bot)
        except Exception:
            pass
        else:
            extra["$message_id"] = msg_id
        msg._handle_i18n(extra)
        return msg

    @classmethod
    async def send(
        cls,
        message: _M,
        fallback: bool | FallbackStrategy = FallbackStrategy.ignore,
        **kwargs: Any,
    ) -> Any:
        """发送一条消息给当前交互用户

        `AlconnaMatcher` 下增加了对 UniMessage 的发送支持

        参数:
            message: 消息内容, 支持 `UniMessage`
            fallback: 若 UniMessage 中的元素无法被解析为当前 adapter 的消息元素时的策略
            kwargs: {ref}`nonebot.adapters.Bot.send` 的参数，
                请参考对应 adapter 的 bot 对象 api
        """
        bot = current_bot.get()
        event = current_event.get()
        _message = await cls.executor.send_wrapper(bot, event, cls.convert(message))
        if isinstance(_message, UniMessage):
            res = await _message.export(bot, fallback)
        else:
            res = _message
        return await bot.send(event=event, message=res, **kwargs)

    @classmethod
    async def finish(
        cls,
        message: _M | None = None,
        fallback: bool | FallbackStrategy = FallbackStrategy.ignore,
        **kwargs,
    ) -> NoReturn:
        """发送一条消息给当前交互用户并结束当前事件响应器

        参数:
            message: 消息内容, 支持 `UniMessage`
            fallback: 若 UniMessage 中的元素无法被解析为当前 adapter 的消息元素时的策略
            kwargs: {ref}`nonebot.adapters.Bot.send` 的参数，
                请参考对应 adapter 的 bot 对象 api
        """
        if message is not None:
            await cls.send(message, fallback=fallback, **kwargs)
        raise FinishedException

    @classmethod
    async def pause(
        cls,
        prompt: _M | None = None,
        fallback: bool | FallbackStrategy = FallbackStrategy.ignore,
        **kwargs,
    ) -> NoReturn:
        """发送一条消息给当前交互用户并暂停事件响应器，在接收用户新的一条消息后继续下一个处理函数

        参数:
            prompt: 消息内容, 支持 `UniMessage`
            fallback: 若 UniMessage 中的元素无法被解析为当前 adapter 的消息元素时的策略
            kwargs: {ref}`nonebot.adapters.Bot.send` 的参数，
                请参考对应 adapter 的 bot 对象 api
        """
        if prompt is not None:
            await cls.send(prompt, fallback=fallback, **kwargs)
        raise PausedException

    @classmethod
    async def reject(
        cls,
        prompt: _M | None = None,
        fallback: bool | FallbackStrategy = FallbackStrategy.ignore,
        **kwargs,
    ) -> NoReturn:
        """最近使用 `got` / `receive` 接收的消息不符合预期，
        发送一条消息给当前交互用户并将当前事件处理流程中断在当前位置，在接收用户新的一个事件后从头开始执行当前处理函数

        参数:
            prompt: 消息内容, 支持 `UniMessage`
            fallback: 若 UniMessage 中的元素无法被解析为当前 adapter 的消息元素时的策略
            kwargs: {ref}`nonebot.adapters.Bot.send` 的参数，
                请参考对应 adapter 的 bot 对象 api
        """
        if prompt is not None:
            await cls.send(prompt, fallback=fallback, **kwargs)
        raise RejectedException

    @classmethod
    async def reject_path(
        cls,
        path: str,
        prompt: _M | None = None,
        fallback: bool | FallbackStrategy = FallbackStrategy.ignore,
        **kwargs,
    ) -> NoReturn:
        """最近使用 `got_path` 接收的消息不符合预期，
        发送一条消息给当前交互用户并将当前事件处理流程中断在当前位置，在接收用户新的一条消息后从头开始执行当前处理函数

        参数:
            path: 参数路径名; "~XX" 时会把 "~" 替换为父级 assign 的 path
            prompt: 消息内容, 支持 `UniMessage`
            fallback: 若 UniMessage 中的元素无法被解析为当前 adapter 的消息元素时的策略
            kwargs: {ref}`nonebot.adapters.Bot.send` 的参数，
                请参考对应 adapter 的 bot 对象 api
        """
        matcher = current_matcher.get()
        matcher.set_target(ALCONNA_ARG_KEY.format(key=merge_path(path, cls.basepath)))
        if prompt is not None:
            await cls.send(prompt, fallback=fallback, **kwargs)
        raise RejectedException

    @classmethod
    async def reject_arg(
        cls,
        key: str,
        prompt: _M | None = None,
        fallback: bool | FallbackStrategy = FallbackStrategy.ignore,
        **kwargs,
    ) -> NoReturn:
        """最近使用 `got` 接收的消息不符合预期，
        发送一条消息给当前交互用户并将当前事件处理流程中断在当前位置，在接收用户新的一条消息后从头开始执行当前处理函数

        参数:
            key: 参数名
            prompt: 消息内容, 支持 `UniMessage`
            fallback: 若 UniMessage 中的元素无法被解析为当前 adapter 的消息元素时的策略
            kwargs: {ref}`nonebot.adapters.Bot.send` 的参数，
                请参考对应 adapter 的 bot 对象 api
        """
        matcher = current_matcher.get()
        matcher.set_target(ARG_KEY.format(key=key))
        if prompt is not None:
            await cls.send(prompt, fallback=fallback, **kwargs)
        raise RejectedException

    @classmethod
    async def reject_receive(
        cls,
        id: str = "",
        prompt: _M | None = None,
        fallback: bool | FallbackStrategy = FallbackStrategy.ignore,
        **kwargs,
    ) -> NoReturn:
        """最近使用 `receive` 接收的消息不符合预期，
        发送一条消息给当前交互用户并将当前事件处理流程中断在当前位置，在接收用户新的一个事件后从头开始执行当前处理函数

        参数:
            id: 消息 id
            prompt: 消息内容, 支持 `UniMessage`
            fallback: 若 UniMessage 中的元素无法被解析为当前 adapter 的消息元素时的策略
            kwargs: {ref}`nonebot.adapters.Bot.send` 的参数，
                请参考对应 adapter 的 bot 对象 api
        """
        matcher = current_matcher.get()
        matcher.set_target(RECEIVE_KEY.format(id=id))
        if prompt is not None:
            await cls.send(prompt, fallback=fallback, **kwargs)
        raise RejectedException

    @classmethod
    @deprecated("Use `nonebot_plugin_waiter.waiter` instead")
    def waiter(cls, parameterless: Iterable[Any] | None = None, keep_session: bool = False):
        """装饰一个函数来创建一个 `Waiter` 对象用以等待用户输入

        函数内需要自行判断输入是否符合预期并返回结果

        参数:
            parameterless: 非参数类型依赖列表
            keep_session: 是否保持会话
        """
        warn("Use `nonebot_plugin_waiter.waiter` instead", DeprecationWarning, stacklevel=2)

        require("nonebot_plugin_waiter")

        from nonebot_plugin_waiter import waiter

        def wrapper(func: _DependentCallable[R]):
            return waiter(["message"], cls, parameterless=parameterless, keep_session=keep_session)(func)

        return wrapper

    @classmethod
    async def prompt(cls, message: _M, timeout: float = 120):
        """等待用户输入并返回结果

        参数:
            message: 提示消息
            timeout: 等待超时时间
        返回值:
            符合条件的用户输入
        """
        require("nonebot_plugin_waiter")

        from nonebot_plugin_waiter import waiter

        await cls.send(message)

        @annotation(event=Event)
        async def wrapper(event: Event):
            return event.get_message()

        wait = waiter(["message"], keep_session=True)(wrapper)

        res = await wait.wait(timeout=timeout)
        if res is None:
            return
        return await UniMessage.generate(message=cast(Message, res))

    @classmethod
    def test(cls, message: str | UniMessage, expected: dict[str, Any] | None = None, prefix: bool = True):
        """测试当前事件响应器

        参数:
            message: 测试消息
            expected: 预期结果
            prefix: 是否给 message 添加命令前缀
        """
        if isinstance(message, str):
            message = UniMessage(message)
        cls._tests.append((message, expected, prefix))
        return cls

    @classmethod
    def _run_tests(cls):
        if not cls._tests:
            return
        cmd = cls.command()
        if cmd is None:
            log("ERROR", Lang.nbp_alc.test.command_unusable(cmd=cls._command_path))
            return
        prefix = ""
        if cmd.prefixes:
            prefix: str = random.choice(cmd.prefixes)  # type: ignore
        has_error = False
        for message, expected, need_pf in cls._tests:
            if need_pf:
                message = UniMessage(prefix) + message
            res = cmd.parse(message)
            if not res.matched:
                log("ERROR", Lang.nbp_alc.test.parse_failed(msg=message, cmd=cls._command_path))
                has_error = True
                continue
            if not expected:
                continue
            for path, expect in expected.items():
                if (val := res.query(path)) != expect:
                    log(
                        "ERROR",
                        Lang.nbp_alc.test.check_failed(arg=path, expected=expect, got=val, cmd=cls._command_path),
                    )
                    has_error = True
        if not has_error:
            log("DEBUG", Lang.nbp_alc.test.passed(cmd=cls._command_path))


def on_alconna(
    command: Alconna | str,
    rule: Rule | T_RuleChecker | None = None,
    skip_for_unmatch: bool = True,
    auto_send_output: bool = False,
    aliases: set[str] | tuple[str, ...] | None = None,
    comp_config: CompConfig | None = None,
    extensions: list[type[Extension] | Extension] | None = None,
    exclude_ext: list[type[Extension] | str] | None = None,
    use_origin: bool = False,
    use_cmd_start: bool = False,
    use_cmd_sep: bool = False,
    permission: Permission | T_PermissionChecker | None = None,
    *,
    handlers: list[T_Handler | Dependent] | None = None,
    temp: bool = False,
    expire_time: datetime | timedelta | None = None,
    priority: int = 1,
    block: bool = False,
    default_state: T_State | None = None,
    _depth: int = 0,
) -> type[AlconnaMatcher]:
    """注册一个事件响应器，并且当消息由指定 Alconna 解析并传出有效结果时响应。

    参数:
        command: Alconna 命令
        rule: 事件响应规则
        skip_for_unmatch: 是否在解析失败时跳过
        auto_send_output: 是否自动发送输出信息并跳过
        aliases: 命令别名
        comp_config: 补全会话配置, 不传入则不启用补全会话
        extensions: 需要加载的匹配扩展
        exclude_ext: 需要排除的匹配扩展
        use_origin: 是否使用未经 to_me 等处理过的消息
        use_cmd_start: 是否使用 nb 全局配置里的命令起始符
        use_cmd_sep: 是否使用 nb 全局配置里的命令分隔符
        permission: 事件响应权限
        handlers: 事件处理函数列表
        temp: 是否为临时事件响应器（仅执行一次）
        expire_time: 事件响应器最终有效时间点，过时即被删除
        priority: 事件响应器优先级
        block: 是否阻止事件向更低优先级传递
        state: 默认 state
    """
    if isinstance(command, str):
        command = AlconnaFormat(command)
    _rule = alconna(
        command,
        skip_for_unmatch,
        auto_send_output,
        comp_config,
        extensions,
        exclude_ext,
        use_origin,
        use_cmd_start,
        use_cmd_sep,
        aliases,
    )
    executor = cast(ExtensionExecutor, list(_rule.checkers)[0].call.executor)  # type: ignore
    params = (
        (ExtensionParam.new(executor),)
        + Matcher.HANDLER_PARAM_TYPES[:-1]
        + (
            AlconnaParam,
            DefaultParam,
        )
    )
    source = get_matcher_source(_depth + 1)
    NewMatcher = type(
        AlconnaMatcher.__name__,
        (AlconnaMatcher,),
        {
            "_source": source,
            "type": "",
            "rule": _rule & rule,
            "permission": Permission() | permission,
            "handlers": (
                [
                    (
                        handler
                        if isinstance(handler, Dependent)
                        else Dependent[Any].parse(call=handler, allow_types=params)
                    )
                    for handler in handlers
                ]
                if handlers
                else []
            ),
            "temp": temp,
            "expire_time": (
                expire_time and (expire_time if isinstance(expire_time, datetime) else datetime.now() + expire_time)
            ),
            "priority": priority,
            "block": block,
            "_default_state": default_state or {},
            "_default_type_updater": None,
            "_default_permission_updater": None,
        },
    )
    matcher: type[AlconnaMatcher] = cast("type[AlconnaMatcher]", NewMatcher)
    matcher.HANDLER_PARAM_TYPES = params
    log("TRACE", f"Define new matcher {NewMatcher}")

    matchers[priority].append(NewMatcher)
    store_matcher(matcher)
    matcher.command = weakref.ref(command)
    matcher._command_path = command.path
    matcher.basepath = ""
    matcher.executor = executor
    command.meta.extra["matcher.source"] = matcher._source
    command.meta.extra["matcher.position"] = (priority, len(matchers[priority]) - 1)

    def remove(wr, selfref=weakref.ref(command.meta), _atomic_removal=_remove_dead_weakref):
        self = selfref()
        if self is not None:
            _atomic_removal(self.extra, wr.key)

    command.meta.extra["matcher"] = weakref.KeyedRef(matcher, remove, "matcher")

    try:
        driver = get_driver()
    except ValueError:
        return matcher
    matcher._tests = []
    driver.on_startup(matcher._run_tests)
    return matcher


def referent(cmd: str | Alconna | None) -> type[AlconnaMatcher] | None:
    """获取 Alconna 对应的 Matcher

    若不存在则返回 None
    """
    if not cmd:
        return
    if isinstance(cmd, str):
        try:
            cmd = command_manager.get_command(cmd)
        except ValueError:
            return
    try:
        if "matcher" in cmd.meta.extra:
            return cmd.meta.extra["matcher"]()
    except KeyError:
        return


def funcommand(
    name: str | None = None,
    prefixes: list[str] | None = None,
    description: str | None = None,
    skip_for_unmatch: bool = True,
    auto_send_output: bool = False,
    extensions: list[type[Extension] | Extension] | None = None,
    exclude_ext: list[type[Extension] | str] | None = None,
    use_origin: bool = False,
    use_cmd_start: bool = False,
    use_cmd_sep: bool = False,
    rule: Rule | T_RuleChecker | None = None,
    permission: Permission | T_PermissionChecker | None = None,
    *,
    handlers: list[T_Handler | Dependent] | None = None,
    temp: bool = False,
    expire_time: datetime | timedelta | None = None,
    priority: int = 1,
    block: bool = False,
    default_state: T_State | None = None,
    _depth: int = 0,
) -> Callable[[Callable[..., MReturn]], type[AlconnaMatcher]]:
    _config: MountConfig = {"raise_exception": False}
    if name:
        _config["command"] = name
    if prefixes:
        _config["prefixes"] = prefixes
    if description:
        _config["description"] = description

    def wrapper(func: Callable[..., MReturn]) -> type[AlconnaMatcher]:
        matcher = on_alconna(
            FuncMounter(func, _config),
            rule,
            skip_for_unmatch,
            auto_send_output,
            extensions=extensions,
            exclude_ext=exclude_ext,
            use_origin=use_origin,
            use_cmd_start=use_cmd_start,
            use_cmd_sep=use_cmd_sep,
            permission=permission,
            handlers=handlers,
            temp=temp,
            expire_time=expire_time,
            priority=priority,
            block=block,
            default_state=default_state,
            _depth=_depth + 1,
        )

        @matcher.handle()
        @annotation(results=AlcExecResult)
        async def handle_func(results: AlcExecResult):
            if res := results.get(func.__name__):
                if isinstance(res, Hashable) and is_awaitable(res):
                    res = await res
                if isinstance(res, (str, Message, MessageSegment, Segment, UniMessage, UniMessageTemplate)):
                    await matcher.send(res, fallback=True)

        return matcher

    return wrapper


class Command(AlconnaString):
    @staticmethod
    def args_gen(pattern: str, types: dict):
        return AlconnaString.args_gen(pattern, {**types, **patterns})

    def build(
        self,
        rule: Rule | T_RuleChecker | None = None,
        skip_for_unmatch: bool = True,
        auto_send_output: bool = False,
        aliases: set[str] | tuple[str, ...] | None = None,
        comp_config: CompConfig | None = None,
        extensions: list[type[Extension] | Extension] | None = None,
        exclude_ext: list[type[Extension] | str] | None = None,
        use_origin: bool = False,
        use_cmd_start: bool = False,
        use_cmd_sep: bool = False,
        permission: Permission | T_PermissionChecker | None = None,
        *,
        handlers: list[T_Handler | Dependent] | None = None,
        temp: bool = False,
        expire_time: datetime | timedelta | None = None,
        priority: int = 1,
        block: bool = False,
        default_state: T_State | None = None,
        _depth: int = 0,
    ):
        params = locals().copy()
        params["_depth"] += 1
        params.pop("self")
        params.pop("__class__", None)
        alc = Alconna(*self.buffer.values(), *self.options, meta=self.meta)
        for action in self.actions:
            alc.bind()(action)
        matcher = on_alconna(alc, **params)
        for key, args, kwargs in self.shortcuts:
            matcher.shortcut(key, args, **kwargs)  # type: ignore
        if self.actions:

            @matcher.handle()
            @annotation(results=AlcExecResult)
            async def handle_actions(results: AlcExecResult):
                for res in results.values():
                    if isinstance(res, Hashable) and is_awaitable(res):
                        res = await res
                    if isinstance(res, (str, Message, MessageSegment, Segment, UniMessage, UniMessageTemplate)):
                        await matcher.send(res, fallback=FallbackStrategy.rollback)

        return matcher


@run_postprocessor
@annotation(matcher=AlconnaMatcher)
def _exit_executor(matcher: AlconnaMatcher):
    matcher.executor.context.clear()
