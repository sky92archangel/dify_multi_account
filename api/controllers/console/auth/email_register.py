from flask import make_response, request
from flask_restx import Resource
from pydantic import BaseModel, Field, field_validator

from configs import dify_config
from constants import DEFAULT_REGISTER_PASSWORD
from constants.languages import get_valid_language, languages
from controllers.console import console_ns
from controllers.console.auth.error import (
    EmailAlreadyInUseError,
    EmailCodeError,
    EmailRegisterLimitError,
    InvalidEmailError,
    InvalidTokenError,
    PasswordMismatchError,
)
from libs.helper import EmailStr, extract_remote_ip
from libs.password import valid_password
from libs.token import (
    set_access_token_to_cookie,
    set_csrf_token_to_cookie,
    set_refresh_token_to_cookie,
)
from models import Account
from services.account_service import AccountService
from services.billing_service import BillingService
from services.errors.account import AccountNotFoundError, AccountRegisterError

from ..error import AccountInFreezeError, EmailSendIpLimitError
from ..wraps import email_password_login_enabled, email_register_enabled, setup_required

DEFAULT_REF_TEMPLATE_SWAGGER_2_0 = "#/definitions/{model}"


class EmailRegisterDirectPayload(BaseModel):
    email: EmailStr = Field(..., description="Email address")
    language: str | None = Field(default=None, description="Language code")
    timezone: str | None = Field(default=None, description="Timezone")


class EmailRegisterSendPayload(BaseModel):
    email: EmailStr = Field(..., description="Email address")
    language: str | None = Field(default=None, description="Language code")


class EmailRegisterValidityPayload(BaseModel):
    email: EmailStr = Field(...)
    code: str = Field(...)
    token: str = Field(...)


class EmailRegisterResetPayload(BaseModel):
    token: str = Field(...)
    new_password: str = Field(...)
    password_confirm: str = Field(...)

    @field_validator("password_confirm")
    @classmethod
    def validate_password_confirm(cls, value: str) -> str:
        if len(value) < 1:
            raise ValueError("Password must not be empty.")
        return value


for model in (EmailRegisterDirectPayload, EmailRegisterSendPayload, EmailRegisterValidityPayload, EmailRegisterResetPayload):
    console_ns.schema_model(model.__name__, model.model_json_schema(ref_template=DEFAULT_REF_TEMPLATE_SWAGGER_2_0))


@console_ns.route("/email-register/send-email")
class EmailRegisterSendEmailApi(Resource):
    @setup_required
    @email_password_login_enabled
    @email_register_enabled
    def post(self):
        args = EmailRegisterSendPayload.model_validate(console_ns.payload)
        normalized_email = args.email.lower()

        ip_address = extract_remote_ip(request)
        if AccountService.is_email_send_ip_limit(ip_address):
            raise EmailSendIpLimitError()
        language = "en-US"
        if args.language in languages:
            language = args.language

        if dify_config.BILLING_ENABLED and BillingService.is_email_in_freeze(normalized_email):
            raise AccountInFreezeError()

        account = AccountService.get_account_by_email_with_case_fallback(args.email)
        token = AccountService.send_email_register_email(email=normalized_email, account=account, language=language)
        return {"result": "success", "data": token}


@console_ns.route("/email-register/validity")
class EmailRegisterCheckApi(Resource):
    @setup_required
    @email_password_login_enabled
    @email_register_enabled
    def post(self):
        args = EmailRegisterValidityPayload.model_validate(console_ns.payload)

        user_email = args.email.lower()

        return {"is_valid": True, "email": user_email, "token": args.token}


@console_ns.route("/email-register")
class EmailRegisterResetApi(Resource):
    @setup_required
    @email_password_login_enabled
    @email_register_enabled
    def post(self):
        args = EmailRegisterResetPayload.model_validate(console_ns.payload)

        # Validate passwords match
        if args.new_password != args.password_confirm:
            raise PasswordMismatchError()

        # Validate token and get register data
        register_data = AccountService.get_email_register_data(args.token)
        if not register_data:
            raise InvalidTokenError()

        # Revoke token to prevent reuse
        AccountService.revoke_email_register_token(args.token)

        email = register_data.get("email", "")
        normalized_email = email.lower()

        account = AccountService.get_account_by_email_with_case_fallback(email)

        if account:
            raise EmailAlreadyInUseError()
        else:
            account = self._create_new_account(normalized_email, args.password_confirm)
            if not account:
                raise AccountNotFoundError()
            token_pair = AccountService.login(account=account, ip_address=extract_remote_ip(request))
            AccountService.reset_login_error_rate_limit(normalized_email)

        response = make_response({"result": "success", "data": token_pair.model_dump()})
        set_access_token_to_cookie(request, response, token_pair.access_token)
        set_refresh_token_to_cookie(request, response, token_pair.refresh_token)
        set_csrf_token_to_cookie(request, response, token_pair.csrf_token)

        return response

    def _create_new_account(self, email: str, password: str) -> Account | None:
        # Create new account if allowed
        account = None
        try:
            account = AccountService.create_account_and_tenant(
                email=email,
                name=email,
                password=password,
                interface_language=languages[0],
            )
        except AccountRegisterError:
            raise AccountInFreezeError()

        return account


@console_ns.route("/email-register/direct")
class EmailRegisterDirectApi(Resource):
    @setup_required
    @email_password_login_enabled
    @email_register_enabled
    def post(self):
        args = EmailRegisterDirectPayload.model_validate(console_ns.payload)
        normalized_email = args.email.lower()

        account = AccountService.get_account_by_email_with_case_fallback(args.email)
        if account:
            raise EmailAlreadyInUseError()

        account = AccountService.create_account_and_tenant(
            email=normalized_email,
            name=normalized_email,
            password=DEFAULT_REGISTER_PASSWORD,
            interface_language=get_valid_language(args.language),
        )

        token_pair = AccountService.login(account=account, ip_address=extract_remote_ip(request))
        AccountService.reset_login_error_rate_limit(normalized_email)

        response = make_response({"result": "success"})
        set_access_token_to_cookie(request, response, token_pair.access_token)
        set_refresh_token_to_cookie(request, response, token_pair.refresh_token)
        set_csrf_token_to_cookie(request, response, token_pair.csrf_token)

        return response
