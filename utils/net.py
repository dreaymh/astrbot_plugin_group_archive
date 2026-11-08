"""外部 URL 安全校验。

消息内容里的图片地址来自群成员，不能直接拿来发请求：否则有人构造一个
指向内网 / 本机的地址，就能让机器人替他去访问。这里只放行解析到公网地
址的 http(s) 链接。
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse

_LOCAL_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", "broadcasthost"}


def is_public_url(url: str, *, allow_private: bool = False) -> bool:
    """判断 URL 是否指向公网地址（默认拒绝内网、回环、保留地址）。"""
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.hostname
    if not host:
        return False
    if allow_private:
        return True
    if host.lower() in _LOCAL_HOSTS:
        return False
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not addr.is_global:
            return False
    return True
