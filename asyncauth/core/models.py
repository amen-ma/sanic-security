import asyncio
import datetime
import uuid

import jwt
from jwt import DecodeError
from sanic.exceptions import ServerError
from sanic.request import Request
from sanic.response import HTTPResponse
from sanic_ipware import get_client_ip
from tortoise import fields, Model

from asyncauth.core.cache import get_cached_session_code
from asyncauth.core.config import config
from asyncauth.lib.smtp import send_email
from asyncauth.lib.twilio import send_sms


class BaseErrorFactory:
    """
    Easily raise or retrieve errors based off of variable values. Validates the ability for a model to be utilized.
    """

    def get(self, model):
        """
        Retrieves an error if certain conditions are met.
        :return: error
        """
        raise NotImplementedError()

    def throw(self, model):
        """
        Retrieves an error and raises it if certain conditions are met.
        :return: error
        """
        error = self.get(model)
        if error:
            raise error


class AuthError(ServerError):
    """
    Base error for all asyncauth related errors.
    """

    def __init__(self, message, code):
        super().__init__(message, code)


class BaseModel(Model):
    """
    Base asyncauth model that all other models derive from. Some important elements to take in consideration is that
    deletion should be done via the 'deleted' variable and filtering it out rather than completely removing it from
    the database. Retrieving asyncauth models should be done via filtering with the 'uid' variable rather then the id
    variable.
    """

    id = fields.IntField(pk=True)
    account = fields.ForeignKeyField('models.Account', null=True)
    uid = fields.UUIDField(unique=True, default=uuid.uuid1, max_length=36)
    date_created = fields.DatetimeField(auto_now_add=True)
    date_updated = fields.DatetimeField(auto_now=True)
    deleted = fields.BooleanField(default=False)

    def json(self):
        raise NotImplementedError()

    class Meta:
        abstract = True

    class NotFoundError(AuthError):
        def __init__(self, message):
            super().__init__(message, 404)

    class DeletedError(AuthError):
        def __init__(self, message):
            super().__init__(message, 404)


class Account(BaseModel):
    """
    Contains all identifiable user information such as username, email, and more. All passwords must be hashed when
    being created in the database using the hash_pw(str) method.
    """
    username = fields.CharField(max_length=45)
    email = fields.CharField(unique=True, max_length=45)
    phone = fields.CharField(unique=True, max_length=20, null=True)
    password = fields.BinaryField()
    disabled = fields.BooleanField(default=False)
    verified = fields.BooleanField(default=False)

    class ErrorFactory(BaseErrorFactory):
        def get(self, model):
            error = None
            if not model:
                error = Account.NotFoundError('This account does not exist.')
            elif model.deleted:
                error = Account.DeletedError('This account has been permanently deleted.')
            elif model.disabled:
                error = Account.DisabledError()
            elif not model.verified:
                error = Account.UnverifiedError()
            return error

    def json(self):
        return {
            'uid': str(self.uid),
            'date_created': str(self.date_created),
            'date_updated': str(self.date_updated),
            'email': self.email,
            'username': self.username,
            'disabled': self.disabled,
            'verified': self.verified
        }

    class AccountError(AuthError):
        def __init__(self, message, code):
            super().__init__(message, code)

    class ExistsError(AccountError):
        def __init__(self):
            super().__init__('Account with this email or phone number already exists.', 409)

    class TooManyCharsError(AccountError):
        def __init__(self):
            super().__init__('Email, username, or phone number is too long.', 400)

    class InvalidEmailError(AccountError):
        def __init__(self):
            super().__init__('Please use a valid email format such as you@mail.com.', 400)

    class DisabledError(AccountError):
        def __init__(self):
            super().__init__("This account has been disabled.", 401)

    class IncorrectPasswordError(AccountError):
        def __init__(self):
            super().__init__('The password provided is incorrect.', 401)

    class UnverifiedError(AccountError):
        def __init__(self):
            super().__init__('Account requires verification.', 401)


class Session(BaseModel):
    """
    Used specifically for client side tracking. For example, an authentication session is stored on the client's browser
    in order to identify the client. All sessions should be created using the SessionFactory().
    """

    expiration_date = fields.DatetimeField( null=True)
    valid = fields.BooleanField(default=True)
    ip = fields.CharField(max_length=16)
    attempts = fields.IntField(default=0)
    code = fields.CharField(max_length=8, null=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cookie = self.__class__.__name__[:4].lower() + 'tkn'
        self.loop = asyncio.get_running_loop()


    def json(self):
        return {
            'uid': str(self.uid),
            'date_created': str(self.date_created),
            'date_updated': str(self.date_updated),
            'expiration_date': str(self.expiration_date),
            'valid': self.valid,
            'attempts': self.attempts
        }

    async def crosscheck_code(self, code: str):
        """
        Used to check if code passed is equivalent to the session code.

        :param code: Code being cross-checked with session code.
        """
        if self.attempts >= 5:
            raise self.MaximumAttemptsError
        elif self.code != code:
            self.attempts += 1
            await self.save(update_fields=['attempts'])
            raise self.CrosscheckError()
        else:
            self.valid = False
            await self.save(update_fields=['valid'])

    async def encode(self, response: HTTPResponse, secure: bool = False, same_site: str = 'lax'):
        """
        Transforms session into jwt and then is stored in a cookie.

        :param response: Response used to store cookie.

        :param secure: If true, connections must be SSL encrypted (aka https).

        :param same_site: Allows you to declare if your cookie should be restricted to a first-party or same-site context.
        """

        payload = {
            'date_created': str(self.date_created),
            'uid': str(self.uid),
            'ip': self.ip
        }
        encoded = await self.loop.run_in_executor(None, jwt.encode, payload, config['AUTH']['secret'], 'HS256')
        response.cookies[self.cookie] = encoded
        response.cookies[self.cookie]['expires'] = self.expiration_date
        response.cookies[self.cookie]['secure'] = secure
        response.cookies[self.cookie]['samesite'] = same_site

    async def decode_raw(self, request: Request):
        """
        Decodes JWT token in cookie to dict.

        :param request: Sanic request parameter.

        :return: raw
        """
        try:
            return await self.loop.run_in_executor(None, jwt.decode, request.cookies.get(self.cookie),
                                                   config['AUTH']['secret'], 'HS256')
        except DecodeError:
            raise Session.DecodeError()

    async def decode(self, request: Request):
        """
        Decodes JWT token in cookie and transforms into session.

        :param request: Sanic request parameter.

        :return: session
        """

        decoded = await self.decode_raw(request)
        return await self.filter(uid=decoded.get('uid')).prefetch_related('account').first()

    class Meta:
        abstract = True

    class ErrorFactory(BaseErrorFactory):
        def get(self, model):
            error = None
            if model is None:
                error = Session.NotFoundError('Session could not be found.')
            elif not model.valid:
                error = Session.InvalidError()
            elif model.deleted:
                error = Session.DeletedError('Session has been deleted.')
            elif model.expiration_date < datetime.datetime.now(datetime.timezone.utc):
                error = Session.ExpiredError()
            return error

    class SessionError(AuthError):
        def __init__(self, message, code):
            super().__init__(message, code)

    class MaximumAttemptsError(SessionError):
        def __init__(self):
            super().__init__('You\'ve reached the maximum amount of attempts.', 401)

    class DecodeError(SessionError):
        def __init__(self):
            super().__init__('Session is not available.', 404)

    class InvalidError(SessionError):
        def __init__(self):
            super().__init__('Session is invalid.', 401)

    class CrosscheckError(SessionError):
        def __init__(self):
            super().__init__('Session crosschecking attempt was incorrect', 401)

    class ExpiredError(SessionError):
        def __init__(self):
            super().__init__('Session has expired', 401)


class SessionFactory:
    """
    Prevents human error when creating sessions.
    """

    def get_ip(self, request: Request):
        """
        Retrieves the ip address of the request.

        :param request: Sanic request.
        """
        proxies = config['AUTH']['proxies'].split(',').strip() if config.has_option('AUTH', 'proxies') else None
        proxy_count = int(config['AUTH']['proxies']) if config.has_option('AUTH', 'proxy_count') else 0
        ip, routable = get_client_ip(request, proxy_trusted_ips=proxies, proxy_count=proxy_count)
        return ip if ip is not None else '0.0.0.0'

    def generate_expiration_date(self, days: int = 1, minutes: int = 0):
        """
        Creates an expiration date. Adds days to current datetime.

        :param days: days to be added to current time.

        :param minutes: minutes to be added to the current time.

        :return: expiration_date
        """
        return datetime.datetime.utcnow() + datetime.timedelta(days=days, minutes=minutes)

    async def _account_via_decoded(self, request: Request, session: Session):
        """
        Extracts account from decoded session. This method was created purely to prevent repetitive code.

        :param request: Sanic request parameter.

        :param session: Session being decoded to retrieve account from

        :return: account
        """
        decoded_session = await session.decode(request)
        return decoded_session.account

    async def get(self, session_type: str, request: Request, account: Account = None):
        """
        Creates and returns a session with all of the fulfilled requirements.

        :param session_type: The type of session being retrieved. Available types are: captcha, verification, and
        authentication.

        :param request: Sanic request parameter.

        :param account:

        :return:
        """
        code = await get_cached_session_code()
        if session_type == 'captcha':
            return await CaptchaSession.create(ip=self.get_ip(request), code=code[:6],
                                               expiration_date=self.generate_expiration_date(minutes=1))
        elif session_type == 'verification':
            account = account if account else await self._account_via_decoded(request, VerificationSession())
            return await VerificationSession.create(code=code, ip=self.get_ip(request), account=account,
                                                    expiration_date=self.generate_expiration_date(minutes=1))
        elif session_type == 'authentication':
            account = account if account else await self._account_via_decoded(request, AuthenticationSession())
            return await AuthenticationSession.create(account=account, ip=self.get_ip(request),
                                                      expiration_date=self.generate_expiration_date(days=30))
        else:
            raise ValueError('Invalid session type.')


class VerificationSession(Session):
    """
    Verifies an account via emailing or texting a code.
    """

    async def text_code(self, code_prefix="Your code is: "):
        """
        Sends account verification code via text.

        :param code_prefix: Message being sent with code, for example "Your code is: ".
        """
        await send_sms(self.account.phone, code_prefix + self.code)

    async def email_code(self, subject="Session Code", code_prefix='Your code is:\n\n '):
        """
        Sends account verification code via email.

        :param code_prefix: Message being sent with code, for example "Your code is: ".

        :param subject: Subject of email being sent with code.
        """
        await send_email(self.account.email, subject, code_prefix + self.code)


class CaptchaSession(Session):
    """
    Validates an client as human by forcing a user to correctly enter a captcha challenge.
    """
    pass

    async def captcha_img(self, request):
        """
        Retrieves image path of captcha.

        :return: captcha_img_path
        """
        decoded_captcha = await CaptchaSession().decode(request)
        return './resources/session-cache/' + decoded_captcha.code + '.png'


class AuthenticationSession(Session):
    class UnknownLocationError(Session.SessionError):
        def __init__(self):
            super().__init__('Session in an unknown location.', 401)

    async def crosscheck_location(self, request):
        """
        Checks if client using session is in a known location (ip address). Prevents cookie jacking.

        :raises UnknownLocationError:
        """

        if not await AuthenticationSession.filter(ip=request_ip(request), account=self.account).exists():
            raise AuthenticationSession.UnknownLocationError()


class Role(BaseModel):
    """
    Assigned to an account to authorize an action. Used for role based authorization.
    """
    name = fields.CharField(max_length=45)

    def json(self):
        return {
            'uid': str(self.uid),
            'date_created': str(self.date_created),
            'date_updated': str(self.date_updated),
            'name': self.name,
        }

    class InsufficientRoleError(AuthError):
        def __init__(self):
            super().__init__('You do not have the required role for this action.', 403)


class Permission(BaseModel):
    """
    Assigned to an account to authorize an action. Used for wildcard based authorization.
    """
    wildcard = fields.CharField(max_length=45)

    def json(self):
        return {
            'uid': str(self.uid),
            'date_created': str(self.date_created),
            'date_updated': str(self.date_updated),
            'wildcard': self.wildcard,
        }

    class InsufficientPermissionError(AuthError):
        def __init__(self):
            super().__init__('You do not have the required permissions for this action.', 403)


class Proxy(BaseModel):
    ip_from = fields.SmallIntField()
    ip_to = fields.SmallIntField()
    proxy_type = fields.CharField(max_length=3)
    country_code = fields.CharField(max_length=2)
    country_name = fields.CharField(max_length=64)
    region_name = fields.CharField(max_length=128, null=True)
    city_name = fields.CharField(max_length=128, null=True)
    isp = fields.CharField(max_length=256, null=True)
    domain = fields.CharField(max_length=128, null=True)
    usage_type = fields.CharField(max_length=11, null=True)
    asn = fields.SmallIntField()
    last_seen = fields.SmallIntField()

    def json(self):
        return {
            'ip_from': self.ip_from,
            'ip_to': self.ip_to,
            'proxy_type': self.proxy_type,
            'country_code': self.country_code,
            'country_name': self.country_name
        }

    class ProhibitedProxyError(AuthError):
        def __init__(self):
            super().__init__('You are attempting to access a resource from a prohibited proxy.', 403)

