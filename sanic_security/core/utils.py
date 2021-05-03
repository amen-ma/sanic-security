import hashlib
import os
from configparser import ConfigParser
from sanic.request import Request
from sanic.response import HTTPResponse, redirect

security_cache_path = "./resources/security-cache"
config = ConfigParser()
config.read("./auth.ini")


def xss_prevention_middleware(request: Request, response: HTTPResponse):
    """
    Adds a header to all responses that prevents cross site scripting.

    Args:
        response (HTTPResponse): Sanic response parameter.
    """
    response.headers["x-xss-protection"] = "1; mode=block"


def https_redirect_middleware(request: Request):
    """
    Redirects all http requests to https.

    Args:
        request (Request): Sanic request parameter.
    """
    if request.url.startswith("http://"):
        url = request.url.replace("http://", "https://", 1)
        return redirect(url)


def hash_pw(password: str):
    """
    Redirects all http requests to https.

    Args:
        password (str): Password to be hashed.

    Returns:
        hashed_password
    """
    return hashlib.pbkdf2_hmac(
        "sha512",
        password.encode("utf-8"),
        config["AUTH"]["SECRET"].encode("utf-8"),
        100000,
    )


def get_ip(request: Request):
    """
    Retrieves ip address from request.

    Args:
        request (Request): Sanic request parameter.

    Returns:
        ip
    """
    return request.remote_addr if request.remote_addr else request.ip


def dir_exists(path):
    """
    Checks if path exists and isn't empty, and creates it if it doesn't.

    Args:
         path: Path being checked.

    Returns:
        exists
    """
    try:
        os.makedirs(path)
        exists = False
    except FileExistsError:
        exists = os.listdir(path)
    return exists
