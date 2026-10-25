"""音乐来源：网易云 / QQ音乐 / 汽水音乐。

统一接口（便于二次开发接入新平台）：
    enabled()                 是否配置了可用来源
    list_tracks() -> [Track]  拉取候选歌曲（歌单优先，否则关键词搜索）
    decorate(track)           选歌后补充信息（如歌词摘句），失败不影响流程

公开 API 均为网页端接口，无需凭据；汽水音乐无公开 API，用配置歌名列表或自定义接口。
"""

import asyncio
import base64
import random
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from astrbot.api import logger

from .http_util import fetch_json

NETEASE_HEADERS = {"Referer": "https://music.163.com/"}
QQ_HEADERS = {"Referer": "https://y.qq.com/"}


@dataclass
class Track:
    """一首歌的最小信息集。"""

    platform: str          # 平台标识：netease / qq / qishui
    platform_name: str     # 平台展示名
    name: str
    artist: str
    album: str = ""
    url: str = ""          # 网页试听链接（可空）
    song_id: str = ""      # 平台内部 ID，用于补全歌词
    lyric_line: str = ""   # 歌词摘句（装饰字段）

    @property
    def key(self) -> str:
        """去重键：同平台同歌名同歌手视为同一首。"""
        return f"{self.platform}:{self.name}:{self.artist}"


# LRC 元数据行：「作词 : …」「Bass : …」等标签行，与「歌名 - 歌手 (…)」式标题行
_META_LINE_RE = re.compile(
    r"^[^\s。！？]{1,8}\s*[：:]|作词|作曲|编曲|混音|母带|和声|和音|配唱|录音|制作",
    re.IGNORECASE,
)
_TITLE_LINE_RE = re.compile(r"^[^。！？]{1,40} - [^。！？]{1,40}\s*[（(]")


def extract_lyric_line(lrc: str, limit: int = 40) -> str:
    """从 LRC 文本取第一句有效歌词（跳过时间轴、元数据与标题行）。"""
    for line in str(lrc).splitlines():
        text = line.rsplit("]", 1)[-1].strip()
        if len(text) < 4 or text.startswith(("[", "<")):
            continue
        if _META_LINE_RE.search(text) or _TITLE_LINE_RE.match(text):
            continue
        return text[:limit]
    return ""


class BaseSource(ABC):
    """平台来源基类：子类只需实现 list_tracks 与可选 decorate。"""

    key = ""
    display = ""

    def __init__(self, config: dict):
        cfg = config.get(self.key, {}) or {}
        self.playlist_id = str(cfg.get("playlist_id") or "").strip()
        self.keywords = str(cfg.get("keywords") or "").strip()

    def enabled(self) -> bool:
        return bool(self.playlist_id or self.keywords)

    @abstractmethod
    async def list_tracks(self) -> list:
        """拉取候选歌曲列表，异常向上抛。"""

    async def decorate(self, track: Track) -> None:
        """选歌后补充信息，默认无操作。"""

    async def pick(self, history: set) -> Track | None:
        """从候选中随机选一首（优先避开近期听过的）。"""
        try:
            tracks = await self.list_tracks()
        except Exception as e:
            logger.warning(f"[每日一听] {self.display} 获取歌曲失败: {e}")
            return None
        if not tracks:
            logger.warning(f"[每日一听] {self.display} 没有可用歌曲，请检查来源配置")
            return None
        fresh = [t for t in tracks if t.key not in history]
        pool = fresh or tracks
        track = random.choice(pool)
        try:
            await self.decorate(track)
        except Exception as e:
            logger.debug(f"[每日一听] {self.display} 补全信息失败: {e}")
        return track


class NeteaseSource(BaseSource):
    """网易云：playlist/detail 与 search/get/web 公开接口。"""

    key = "netease"
    display = "网易云"

    async def list_tracks(self) -> list:
        if self.playlist_id:
            data = await fetch_json(
                "https://music.163.com/api/playlist/detail",
                params={"id": self.playlist_id},
                headers=NETEASE_HEADERS,
            )
            raw = (data.get("playlist") or {}).get("tracks") or []
            return [
                Track(
                    platform=self.key,
                    platform_name=self.display,
                    name=str(t.get("name") or ""),
                    artist=((t.get("artists") or [{}])[0].get("name") or ""),
                    album=(t.get("album") or {}).get("name") or "",
                    url=f"https://music.163.com/song?id={t.get('id')}",
                    song_id=str(t.get("id") or ""),
                )
                for t in raw
                if t.get("name")
            ]
        data = await fetch_json(
            "https://music.163.com/api/search/get/web",
            params={"s": self.keywords, "type": 1, "limit": 30},
            headers=NETEASE_HEADERS,
        )
        raw = (data.get("result") or {}).get("songs") or []
        return [
            Track(
                platform=self.key,
                platform_name=self.display,
                name=str(t.get("name") or ""),
                artist=((t.get("artists") or [{}])[0].get("name") or ""),
                album=(t.get("album") or {}).get("name") or "",
                url=f"https://music.163.com/song?id={t.get('id')}",
                song_id=str(t.get("id") or ""),
            )
            for t in raw
            if t.get("name")
        ]

    async def decorate(self, track: Track) -> None:
        if not track.song_id:
            return
        data = await fetch_json(
            "https://music.163.com/api/song/lyric",
            params={"id": track.song_id, "lv": 1, "kv": 1, "tv": -1},
            headers=NETEASE_HEADERS,
        )
        track.lyric_line = extract_lyric_line((data.get("lrc") or {}).get("lyric") or "")


class QQSource(BaseSource):
    """QQ音乐：.fcg 公开接口，需携带 Referer。"""

    key = "qq"
    display = "QQ音乐"

    def _to_track(self, s: dict) -> Track:
        mid = str(s.get("songmid") or s.get("mid") or "")
        album = s.get("albumname") or (s.get("album") or {}).get("name") or ""
        return Track(
            platform=self.key,
            platform_name=self.display,
            name=str(s.get("songname") or s.get("name") or ""),
            artist=(s.get("singer") or [{}])[0].get("name") or "",
            album=album,
            url=f"https://y.qq.com/n/ryqq/songDetail/{mid}" if mid else "",
            song_id=mid,
        )

    async def list_tracks(self) -> list:
        if self.playlist_id:
            data = await fetch_json(
                "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg",
                params={
                    "type": 1, "json": 1, "utf8": 1, "onlysong": 0,
                    "disstid": self.playlist_id, "format": "json",
                },
                headers=QQ_HEADERS,
            )
            raw = ((data.get("cdlist") or [{}])[0]).get("songlist") or []
        else:
            try:
                raw = await self._search_legacy()
            except Exception as e:
                # 旧搜索接口对部分关键词会 500，回退到 musicu.fcg 新接口
                logger.warning(f"[每日一听] QQ音乐旧搜索接口失败（{e}），改用 musicu.fcg")
                raw = await self._search_musicu()
        return [t for t in (self._to_track(s) for s in raw) if t.name]

    async def _search_legacy(self) -> list:
        data = await fetch_json(
            "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
            params={"w": self.keywords, "format": "json", "n": 30},
            headers=QQ_HEADERS,
        )
        return ((data.get("data") or {}).get("song") or {}).get("list") or []

    async def _search_musicu(self) -> list:
        # 该接口有按 IP 的频控（code=2001），空结果时逐次加大等待再试
        for attempt, wait in enumerate((0, 3, 8)):
            if wait:
                await asyncio.sleep(wait)
            data = await fetch_json(
                "https://u.y.qq.com/cgi-bin/musicu.fcg",
                payload={
                    "music.search.SearchCgiService": {
                        "method": "DoSearchForQQMusicDesktop",
                        "module": "music.search.SearchCgiService",
                        "param": {
                            "search_type": 0, "query": self.keywords,
                            "page_num": 1, "num_per_page": 30,
                        },
                    }
                },
                headers=QQ_HEADERS,
            )
            svc = data.get("music.search.SearchCgiService") or {}
            body = (svc.get("data") or {}).get("body") or {}
            songs = (body.get("song") or {}).get("list") or []
            if songs:
                return songs
            code = svc.get("code")
            if code:
                logger.warning(f"[每日一听] QQ音乐 musicu.fcg 返回 code={code}，稍后重试")
        raise RuntimeError(f"musicu.fcg 无结果（code={code}），可能触发频控")

    async def decorate(self, track: Track) -> None:
        if not track.song_id:
            return
        data = await fetch_json(
            "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
            params={"songmid": track.song_id, "g_tk": 5381, "format": "json"},
            headers=QQ_HEADERS,
        )
        lyric = base64.b64decode(data.get("lyric") or "").decode("utf-8", "ignore")
        track.lyric_line = extract_lyric_line(lyric)


class QishuiSource(BaseSource):
    """汽水音乐：无公开 API。歌名列表为必选来源，api_url 可选提供结构化歌曲。"""

    key = "qishui"
    display = "汽水音乐"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get(self.key, {}) or {}
        self.song_list = str(cfg.get("song_list") or "")
        self.api_url = str(cfg.get("api_url") or "").strip()

    def enabled(self) -> bool:
        return bool(self.api_url or self.song_list.strip())

    async def list_tracks(self) -> list:
        if self.api_url:
            try:
                return self._from_api(await fetch_json(self.api_url))
            except Exception as e:
                logger.warning(f"[每日一听] 汽水自定义接口失败，回退歌名列表: {e}")
        return self._from_text()

    @staticmethod
    def _from_api(data) -> list:
        raw = data.get("songs") if isinstance(data, dict) else data
        return [
            Track(
                platform="qishui",
                platform_name="汽水音乐",
                name=str(t.get("name") or ""),
                artist=str(t.get("artist") or "未知歌手"),
            )
            for t in (raw or [])
            if isinstance(t, dict) and t.get("name")
        ]

    def _from_text(self) -> list:
        tracks = []
        for line in self.song_list.splitlines():
            line = line.strip()
            if not line:
                continue
            name, _, artist = line.partition(" - ")
            tracks.append(
                Track(
                    platform=self.key,
                    platform_name=self.display,
                    name=name.strip(),
                    artist=artist.strip() or "未知歌手",
                )
            )
        return tracks


SOURCE_CLASSES = (NeteaseSource, QQSource, QishuiSource)


def build_sources(config: dict) -> list:
    """按 listen.platforms 配置的顺序构建可用来源；未配置来源的平台自动跳过。"""
    order = list((config.get("listen", {}) or {}).get("platforms") or [])
    if not order:
        order = [s.key for s in SOURCE_CLASSES]
    by_key = {s.key: s for s in SOURCE_CLASSES}
    sources = []
    for key in order:
        cls = by_key.get(key)
        if cls is None:
            continue
        source = cls(config)
        if source.enabled():
            sources.append(source)
    return sources
