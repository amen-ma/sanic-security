import asyncio
import functools
import shutil

import aioIP2Proxy
import aiofiles
import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sanicsecurity.core.config import config
from sanicsecurity.core.models import AuthError
from sanicsecurity.core.utils import path_exists, get_ip

ip2proxy_database = aioIP2Proxy.IP2Proxy()


class ProxyDetectedError(AuthError):
    def __init__(self):
        super(ProxyDetectedError, self).__init__('Attempting to access a resource from a forbidden proxy.', 403)


async def cache_ip2proxy_database():
    """
    Caches a new IP2Proxy database.
    """
    code = config['IP2PROXY']['code']
    key = config['IP2PROXY']['key']
    loop = asyncio.get_running_loop()
    async with aiohttp.ClientSession() as session:
        url = "https://www.ip2location.com/download/?token={0}&file={1}".format(key, code)
        async with session.get(url) as resp:
            zip_path = './resources/security-cache/ip2proxy/ip2proxy.zip'
            async with aiofiles.open(zip_path, mode="wb") as f:
                try:
                    await f.write(await resp.read())
                    await loop.run_in_executor(None, shutil.unpack_archive, zip_path,
                                               './resources/security-cache/ip2proxy')
                except shutil.ReadError:
                    raise shutil.ReadError('Unzipping has failed due to the download limit or incorrect credentials.')


async def initialize_ip2proxy():
    """
    Initializes a async cron job that runs every 00:15 GMT to refresh the IP2Proxy database.
    """
    if not path_exists('./resources/security-cache/ip2proxy'):
        await cache_ip2proxy_database()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(cache_ip2proxy_database, 'cron', minute='15', hour='0', month='*', week='*', day='*')
    scheduler.start()


async def proxy_detection(ip: str):
    await ip2proxy_database.open('./resources/security-cache/ip2proxy/' + config['IP2PROXY']['bin'])
    if await ip2proxy_database.is_proxy(ip) > 0:
        raise ProxyDetectedError()
    await ip2proxy_database.close()


def detect_proxy():
    """
    Authenticates the client's current authentication session.

    :raises AccountError:

    :raises SessionError:

    :return: func(request, authentication_session, *args, **kwargs)
    """

    def wrapper(func):
        @functools.wraps(func)
        async def wrapped(request, *args, **kwargs):
            await proxy_detection_middleware(request)
            return await func(request, *args, **kwargs)

        return wrapped

    return wrapper


async def proxy_detection_middleware(request):
    await proxy_detection(get_ip(request))
