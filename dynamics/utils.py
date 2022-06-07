import logging
import pickle
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from uuid import UUID

from .exceptions import DynamicsException


try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from .typing import TYPE_CHECKING, Any, Awaitable, Callable, List, Optional, P, T, Type


if TYPE_CHECKING:
    from . import DynamicsClient


__all__ = [
    "to_dynamics_date_format",
    "from_dynamics_date_format",
    "sentinel",
    "is_valid_uuid",
    "SQLiteCache",
    "cache",
    "error_simplification_available",
    "to_coroutine",
]


logger = logging.getLogger(__name__)


class sentinel:  # pylint: disable=C0103
    """Sentinel value."""


def is_valid_uuid(value: str):
    try:
        uuid = UUID(value)
        return str(uuid) == value
    except Exception:  # pylint: disable=W0703
        return False


def to_dynamics_date_format(date: datetime, from_timezone: str = None) -> str:
    """Convert a datetime-object to a Dynamics compatible ISO formatted date string.

    :param date: Datetime object.
    :param from_timezone: Time zone name from the IANA Time Zone Database the date is in.
                          Dynamics dates are in UCT, so timezoned values need to be converted to it.
    """

    if from_timezone is not None and date.tzinfo is None:
        date: datetime = date.replace(tzinfo=ZoneInfo(from_timezone))

    if date.tzinfo is not None:
        date -= date.utcoffset()

    return date.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def from_dynamics_date_format(date: str, to_timezone: str = "UCT") -> datetime:
    """Convert a Dynamics compatible ISO formatted date string to a datetime-object.

    :param date: Date string in form: YYYY-mm-ddTHH:MM:SSZ
    :param to_timezone: Time zone name from the IANA Time Zone Database to convert the date to.
                        This won't add 'tzinfo', instead the actual time part will be changed from UCT
                        to what the time is at 'to_timezone'.
    """
    local_time = datetime.fromisoformat(date.replace("Z", "")).replace(tzinfo=ZoneInfo(to_timezone))
    local_time += local_time.utcoffset()
    local_time = local_time.replace(tzinfo=None)
    return local_time


def sqlite_method(method: Callable[P, T]) -> Callable[P, T]:
    """Wrapped method is executed under an open sqlite3 connection.
    Method's class should contain a 'self.connection_string' that is used to make the connection.
    This decorator then updates a 'self.con' object inside the class to the current connection.
    After the method is finished, or if it raises an exception, the connection is closed and the
    return value or exception propagated.
    """

    @wraps(method)
    def inner(*args: P.args, **kwargs: P.kwargs) -> T:
        self = args[0]
        self.con = sqlite3.connect(self.connection_string)
        self._apply_pragma()  # pylint: disable=W0212

        try:
            value = method(*args, **kwargs)
            self.con.commit()
        except Exception as sqlerror:
            self.con.execute(self._set_pragma.format("optimize"))  # pylint: disable=W0212
            self.con.close()
            raise sqlerror

        self.con.execute(self._set_pragma.format("optimize"))  # pylint: disable=W0212
        self.con.close()
        return value

    return inner


class SQLiteCache:
    """Dymmy cache to use if Django's cache is not installed."""

    DEFAULT_TIMEOUT = 300
    DEFAULT_PRAGMA = {
        "mmap_size": 2**26,  # https://www.sqlite.org/pragma.html#pragma_mmap_size
        "cache_size": 8192,  # https://www.sqlite.org/pragma.html#pragma_cache_size
        "wal_autocheckpoint": 1000,  # https://www.sqlite.org/pragma.html#pragma_wal_autocheckpoint
        "auto_vacuum": "none",  # https://www.sqlite.org/pragma.html#pragma_auto_vacuum
        "synchronous": "off",  # https://www.sqlite.org/pragma.html#pragma_synchronous
        "journal_mode": "wal",  # https://www.sqlite.org/pragma.html#pragma_journal_mode
        "temp_store": "memory",  # https://www.sqlite.org/pragma.html#pragma_temp_store
    }

    _create_sql = "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value BLOB, exp REAL)"
    _create_index_sql = "CREATE UNIQUE INDEX IF NOT EXISTS cache_key ON cache(key)"
    _set_pragma = "PRAGMA {}"
    _set_pragma_equal = "PRAGMA {}={}"

    _get_sql = "SELECT value, exp FROM cache WHERE key = :key"
    _set_sql = (
        "INSERT INTO cache (key, value, exp) VALUES (:key, :value, :exp) "
        "ON CONFLICT(key) DO UPDATE SET value = :value, exp = :exp"
    )
    _delete_sql = "DELETE FROM cache WHERE key = :key"
    _clear_sql = "DELETE FROM cache"

    def __init__(self, *, filename: str = "dynamics.cache", path: str = None):
        """Create a cache using sqlite3.

        :param filename: Cache file name.
        :param path: Path string to the wanted db location. If None, use current directory.
        """

        filepath = filename if path is None else str(Path(path) / filename)
        self.connection_string = f"{filepath}:?mode=memory&cache=shared"

        self.con = sqlite3.connect(self.connection_string)
        self.con.execute(self._create_sql)
        self.con.execute(self._create_index_sql)
        self.con.commit()
        self.con.close()

    @staticmethod
    def _exp_timestamp(timeout: int = DEFAULT_TIMEOUT) -> float:
        return (datetime.now(timezone.utc) + timedelta(seconds=timeout)).timestamp()

    @staticmethod
    def _stream(value: Any) -> bytes:
        return pickle.dumps(value)

    @staticmethod
    def _unstream(value: bytes) -> Any:
        return pickle.loads(value)

    def _apply_pragma(self):
        for key, value in self.DEFAULT_PRAGMA.items():
            self.con.execute(self._set_pragma_equal.format(key, value))

    @sqlite_method
    def get(self, key: str, default: Any = None) -> Any:
        result: Optional[tuple] = self.con.execute(self._get_sql, {"key": key}).fetchone()

        if result is None:
            return default

        if datetime.utcnow() >= datetime.utcfromtimestamp(result[1]):
            self.con.execute(self._delete_sql, {"key": key})
            return default

        return self._unstream(result[0])

    @sqlite_method
    def set(self, key: str, value: Any, timeout: int = DEFAULT_TIMEOUT) -> None:
        data = {"key": key, "value": self._stream(value), "exp": self._exp_timestamp(timeout)}
        self.con.execute(self._set_sql, data)

    @sqlite_method
    def clear(self) -> None:
        self.con.execute(self._clear_sql)


try:
    from django.core.cache import cache
except ImportError:
    cache = SQLiteCache()


def error_simplification_available(func: Callable[P, T]) -> Callable[P, T]:
    """Errors in the function decorated with this decorator can be simplified to just a
    DynamicsException with default error message using the keyword: 'simplify_errors'.
    This is useful if you want to hide error details from frontend users.

    You can use the 'raise_separately' keyword to list exception types to exclude from this
    simplification, if separate handling is needed.

    :param func: Decorated function.
    """

    @wraps(func)
    def inner(*args: P.args, **kwargs: P.kwargs) -> T:
        simplify_errors: bool = kwargs.pop("simplify_errors", False)
        raise_separately: List[Type[Exception]] = kwargs.pop("raise_separately", [])

        try:
            return func(*args, **kwargs)
        except Exception as error:  # pylint: disable=W0703
            logger.warning(error)
            if not simplify_errors or any(isinstance(error, exception) for exception in raise_separately):
                raise error
            self: "DynamicsClient" = args[0]
            raise DynamicsException(self.simplified_error_message) from error

    return inner


def to_coroutine(func: Callable[P, T]) -> Callable[P, Awaitable[T]]:
    """Convert passed callable into a coroutine."""

    @wraps(func)
    async def wrapper(*args: P.args, **kw: P.kwargs) -> Any:
        return func(*args, **kw)

    return wrapper
