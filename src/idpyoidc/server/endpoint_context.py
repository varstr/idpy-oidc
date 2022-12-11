import json
import logging
from typing import Any
from typing import Callable
from typing import Optional
from typing import Union

from cryptojwt import KeyJar
from jinja2 import Environment
from jinja2 import FileSystemLoader
from requests import request

from idpyoidc.context import OidcContext
from idpyoidc.message.oidc import ProviderConfigurationResponse
from idpyoidc.server.configure import OPConfiguration
from idpyoidc.server.scopes import SCOPE2CLAIMS
from idpyoidc.server.scopes import Scopes
from idpyoidc.server.session.manager import SessionManager
from idpyoidc.server.template_handler import Jinja2TemplateHandler
from idpyoidc.server.util import get_http_params
from idpyoidc.server.work_environment.oauth2 import WorkEnvironment as OAUTH2_Env
from idpyoidc.server.work_environment.oidc import WorkEnvironment as OIDC_Env
from idpyoidc.util import importer
from idpyoidc.util import rndstr

logger = logging.getLogger(__name__)


def get_provider_capabilities(conf, endpoints):
    _cap = conf.get("capabilities", {})
    if _cap is None:
        _cap = {}

    for endpoint, endpoint_instance in endpoints.items():
        if endpoint in ["webfinger", "provider_config"]:
            continue

        for key, val in endpoint_instance.get_provider_info_attributes().items():
            if key not in _cap:
                _cap[key] = val

    return _cap


def init_user_info(conf, cwd: str):
    kwargs = conf.get("kwargs", {})

    if isinstance(conf["class"], str):
        return importer(conf["class"])(**kwargs)

    return conf["class"](**kwargs)


def init_service(conf, upstream_get=None):
    kwargs = conf.get("kwargs", {})

    if upstream_get:
        kwargs["upstream_get"] = upstream_get

    if isinstance(conf["class"], str):
        try:
            return importer(conf["class"])(**kwargs)
        except TypeError as err:
            logger.error("Could not init service class: {}".format(conf["class"]), err)
            raise

    return conf["class"](**kwargs)


def get_token_handler_args(conf: dict) -> dict:
    """

    :param conf: The configuration
    :rtype: dict
    """
    th_args = conf.get("token_handler_args", None)
    if not th_args:
        th_args = {
            typ: {"lifetime": tid}
            for typ, tid in [("code", 600), ("token", 3600), ("refresh", 86400)]
        }

    return th_args


class EndpointContext(OidcContext):
    parameter = {
        "args": {},
        # "authn_broker": AuthnBroker,
        # "authz": AuthzHandling,
        "cdb": "DICT_TYPE",
        "conf": {},
        # "cookie_handler": None,
        "cwd": "",
        "endpoint_to_authn_method": {},
        "httpc_params": {},
        # "idtoken": IDToken,
        "issuer": "",
        "jti_db": {},
        "jwks_uri": "",
        "keyjar": KeyJar,
        "login_hint_lookup": None,
        "login_hint2acrs": {},
        "par_db": {},
        "provider_info": {},
        "registration_access_token": {},
        "scope2claims": {},
        # "session_db": {},
        "session_manager": SessionManager,
        "sso_ttl": None,
        "symkey": "",
        "token_args_methods": [],
        # "userinfo": UserInfo,
        "client_authn_method": {},
    }

    def __init__(
            self,
            conf: Union[dict, OPConfiguration],
            upstream_get: Callable,
            cwd: Optional[str] = "",
            cookie_handler: Optional[Any] = None,
            httpc: Optional[Any] = None,
            server_type: Optional[str] = '',
            entity_id: Optional[str] = "",
            keyjar: Optional[KeyJar] = None
    ):
        _id = entity_id or conf.get("issuer", "")
        OidcContext.__init__(self, conf, entity_id=_id)
        self.conf = conf
        self.upstream_get = upstream_get

        if not server_type or server_type == "oidc":
            self.work_environment = OIDC_Env()
        elif server_type == "oauth2":
            self.work_environment = OAUTH2_Env()
        else:
            raise ValueError(f"Unknown server type: {server_type}")

        _client_db = conf.get("client_db")
        if _client_db:
            logger.debug(f"Loading client db using: {_client_db}")
            self.cdb = importer(_client_db["class"])(**_client_db["kwargs"])
        else:
            logger.debug("No special client db, will use memory based dictionary")
            self.cdb = {}

        # For my Dev environment
        self.jti_db = {}
        self.registration_access_token = {}
        # self.session_db = {}

        self.cwd = cwd

        # Default values, to be changed below depending on configuration
        # arguments for endpoints add-ons
        self.add_on = {}
        self.args = {}
        self.authn_broker = None
        self.authz = None
        self.cookie_handler = cookie_handler
        self.claims_interface = None
        self.endpoint_to_authn_method = {}
        self.httpc = httpc or request
        self.idtoken = None
        self.issuer = ""
        # self.jwks_uri = None
        self.login_hint_lookup = None
        self.login_hint2acrs = None
        self.par_db = {}
        self.provider_info = {}
        self.remove_token = None
        self.scope2claims = conf.get("scopes_to_claims", SCOPE2CLAIMS)
        self.session_manager = None
        self.sso_ttl = 14400  # 4h
        self.symkey = rndstr(24)
        self.template_handler = None
        self.token_args_methods = []
        self.userinfo = None
        self.client_authn_method = {}

        for param in [
            "issuer",
            "sso_ttl",
            "symkey",
            "client_authn",
            # "id_token_schema",
        ]:
            try:
                setattr(self, param, conf[param])
            except KeyError:
                pass

        self.th_args = get_token_handler_args(conf)

        # session db
        self._sub_func = {}
        self.do_sub_func()

        _handler = conf.get("template_handler")
        if _handler:
            self.template_handler = _handler
        else:
            _loader = conf.get("template_loader")

            if _loader is None:
                _template_dir = conf.get("template_dir")
                if _template_dir:
                    _loader = Environment(loader=FileSystemLoader(_template_dir), autoescape=True)

            if _loader:
                self.template_handler = Jinja2TemplateHandler(_loader)

        # # self.setup = {}
        # _keys_conf = conf.get("key_conf")
        # if _keys_conf:
        #     jwks_uri_path = _keys_conf["uri_path"]
        #
        #     if self.issuer.endswith("/"):
        #         self.jwks_uri = "{}{}".format(self.issuer, jwks_uri_path)
        #     else:
        #         self.jwks_uri = "{}/{}".format(self.issuer, jwks_uri_path)

        for item in [
            "cookie_handler",
            "authentication",
            "id_token",
        ]:
            _func = getattr(self, "do_{}".format(item), None)
            if _func:
                _func()

        for item in ["login_hint2acrs"]:
            _func = getattr(self, "do_{}".format(item), None)
            if _func:
                _func()

        # which signing/encryption algorithms to use in what context
        self.jwx_def = {}

        # The HTTP clients request arguments
        _cnf = conf.get("httpc_params")
        if _cnf:
            self.httpc_params = get_http_params(_cnf)
        else:  # Backward compatibility
            self.httpc_params = {"verify": conf.get("verify_ssl", True)}

        self.set_scopes_handler()
        self.dev_auth_db = None
        _interface = conf.get("claims_interface")
        if _interface:
            self.claims_interface = init_service(_interface, self.upstream_get)

        if isinstance(conf, OPConfiguration):
            conf = conf.conf
        _supports = self.supports()
        self.keyjar = self.work_environment.load_conf(conf, supports=_supports, keyjar=keyjar)
        self.provider_info = self.work_environment.provider_info(_supports)
        self.provider_info['issuer'] = self.issuer

    def new_cookie(self, name: str, max_age: Optional[int] = 0, **kwargs):
        cookie_cont = self.cookie_handler.make_cookie_content(
            name=name, value=json.dumps(kwargs), max_age=max_age
        )
        return cookie_cont

    def set_scopes_handler(self):
        _spec = self.conf.get("scopes_handler")
        if _spec:
            _kwargs = _spec.get("kwargs", {})
            _cls = importer(_spec["class"])
            self.scopes_handler = _cls(self.upstream_get, **_kwargs)
        else:
            self.scopes_handler = Scopes(
                self.upstream_get,
                allowed_scopes=self.conf.get("allowed_scopes"),
                scopes_to_claims=self.conf.get("scopes_to_claims"),
            )

    def do_add_on(self, endpoints):
        _add_on_conf = self.conf.get("add_on")
        if _add_on_conf:
            for spec in _add_on_conf.values():
                if isinstance(spec["function"], str):
                    _func = importer(spec["function"])
                else:
                    _func = spec["function"]
                _func(endpoints, **spec["kwargs"])

    def do_login_hint2acrs(self):
        _conf = self.conf.get("login_hint2acrs")

        if _conf:
            self.login_hint2acrs = init_service(_conf)
        else:
            self.login_hint2acrs = None

    def do_userinfo(self):
        _conf = self.conf.get("userinfo")
        if _conf:
            if self.session_manager:
                self.userinfo = init_user_info(_conf, self.cwd)
                self.session_manager.userinfo = self.userinfo
            else:
                logger.warning("Cannot init_user_info if no session manager was provided.")

    def do_cookie_handler(self):
        _conf = self.conf.get("cookie_handler")
        if _conf:
            if not self.cookie_handler:
                self.cookie_handler = init_service(_conf)

    def do_sub_func(self) -> None:
        """
        Loads functions that creates subject "sub" values

        :return: string
        """
        ses_par = self.conf.get("session_params") or {}
        sub_func = ses_par.get("sub_func") or {}
        for key, args in sub_func.items():
            if "class" in args:
                self._sub_func[key] = init_service(args)
            elif "function" in args:
                if isinstance(args["function"], str):
                    self._sub_func[key] = importer(args["function"])
                else:
                    self._sub_func[key] = args["function"]

    def set_remember_token(self):
        ses_par = self.conf.get("session_params") or {}

        self.session_manager.remove_inactive_token = ses_par.get("remove_inactive_token", False)

        _rm = ses_par.get("remember_token", {})
        if "class" in _rm:
            _kwargs = _rm.get("kwargs", {})
            self.session_manager.remember_token = init_service(_rm["class"], **_kwargs)
        elif "function" in _rm:
            if isinstance(_rm["function"], str):
                self.session_manager.remember_token = importer(_rm["function"])
            else:
                self.session_manager.remember_token = _rm["function"]

    def do_login_hint_lookup(self):
        _conf = self.conf.get("login_hint_lookup")
        if _conf:
            _userinfo = None
            _kwargs = _conf.get("kwargs")
            if _kwargs:
                _userinfo_conf = _kwargs.get("userinfo")
                if _userinfo_conf:
                    _userinfo = init_user_info(_userinfo_conf, self.cwd)

            if _userinfo is None:
                _userinfo = self.userinfo

            self.login_hint_lookup = init_service(_conf)
            self.login_hint_lookup.userinfo = _userinfo

    def supports(self):
        res = {}
        if self.upstream_get:
            for endpoint in self.upstream_get('endpoints').values():
                res.update(endpoint.supports())
        res.update(self.work_environment.supports())
        return res

    def set_provider_info(self):
        prefers = self.work_environment.prefer
        supported = self.supports()
        _info = {'issuer': self.issuer, 'version': "3.0"}

        for endp in self.upstream_get('endpoints').values():
            if endp.endpoint_name:
                _info[endp.endpoint_name] = endp.full_path

        for key, spec in ProviderConfigurationResponse.c_param.items():
            _val = prefers.get(key, None)
            if not _val and _val != False:
                _val = supported.get(key, None)
                if not _val and _val != False:
                    continue
            _info[key] = _val

        # acr_values
        if 'acr_values_supported' not in _info:
            if self.authn_broker:
                acr_values = self.authn_broker.get_acr_values()
                if acr_values is not None:
                    _info["acr_values_supported"] = acr_values

        self.provider_info = _info

    def get_preference(self, claim, default=None):
        return self.work_environment.get_preference(claim, default=default)

    def set_preference(self, key, value):
        self.work_environment.set_preference(key, value)

    def get_usage(self, claim, default: Optional[str] = None):
        return self.work_environment.get_usage(claim, default)

    def set_usage(self, claim, value):
        return self.work_environment.set_usage(claim, value)
