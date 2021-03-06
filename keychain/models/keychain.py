# -*- coding: utf-8 -*-
# © 2016 Akretion Mourad EL HADJ MIMOUNE, David BEAL, Raphaël REVERDY
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).
from functools import wraps

import logging
import json

from odoo import models, fields, api
from odoo.exceptions import ValidationError, UserError
from odoo.tools.config import config
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet, MultiFernet, InvalidToken
except ImportError as err:
    _logger.debug(err)


def implemented_by_keychain(func):
    """Call a prefixed function based on 'namespace'."""
    @wraps(func)
    def wrapper(cls, *args, **kwargs):
        fun_name = func.__name__
        fun = '_%s%s' % (cls.namespace, fun_name)
        if not hasattr(cls, fun):
            fun = '_default%s' % (fun_name)
        return getattr(cls, fun)(*args, **kwargs)
    return wrapper


class KeychainAccount(models.Model):
    """Manage all accounts of external systems in one place."""

    _name = 'keychain.account'

    name = fields.Char(required=True, help="Humain readable label")
    technical_name = fields.Char(
        required=True,
        help="Technical name. Must be unique")
    namespace = fields.Selection(selection=[],
                                 help="Type of account",
                                 required=True)
    environment = fields.Char(
        required=False,
        help="'prod', 'dev', etc. or empty (for all)"
    )
    login = fields.Char(help="Login")
    clear_password = fields.Char(
        help="Password. Leave empty if no changes",
        inverse='_inverse_set_password',
        compute='_compute_password',
        store=False)
    password = fields.Char(
        help="Password is derived from clear_password",
        readonly=True)
    data = fields.Text(help="Additionnal data as json")

    def _compute_password(self):
        # Only needed in v8 for _description_searchable issues
        return True

    def _get_password(self):
        """Password in clear text."""
        try:
            return self._decode_password(self.password)
        except UserError as warn:
            raise UserError(_(
                "%s \n"
                "Account: %s %s %s " % (
                    warn,
                    self.login, self.name, self.technical_name
                )
            ))

    def get_data(self):
        """Data in dict form."""
        return self._parse_data(self.data)

    @api.constrains('data')
    def _check_data(self):
        """Ensure valid input in data field."""
        for account in self:
            if account.data:
                parsed = account._parse_data(account.data)
                if not account._validate_data(parsed):
                    raise ValidationError(_("Data not valid"))

    def _inverse_set_password(self):
        """Encode password from clear text."""
        # inverse function
        for rec in self:
            rec.password = rec._encode_password(
                rec.clear_password, rec.environment)

    @api.model
    def retrieve(self, domain):
        """Search accounts for a given domain.

        Environment is added by this function.
        Use this instead of search() to benefit from environment filtering.
        Use user.has_group() and suspend_security() before
        calling this method.
        """
        domain.append(['environment', 'in', self._retrieve_env()])
        return self.search(domain)

    @api.multi
    def write(self, vals):
        """At this time there is no namespace set."""
        if not vals.get('data') and not self.data:
            vals['data'] = self._serialize_data(self._init_data())
        return super(KeychainAccount, self).write(vals)

    @implemented_by_keychain
    def _validate_data(self, data):
        """Ensure data is valid according to the namespace.

        How to use:
        - Create a method prefixed with your namespace
        - Put your validation logic inside
        - Return true if data is valid for your usage

        This method will be called on write().
        If false is returned an user error will be raised.

        Example:
        def _hereismynamspace_validate_data():
            return len(data.get('some_param', '') > 6)

        @params data dict
        @returns boolean
        """
        pass

    def _default_validate_data(self, data):
        """Default validation.

        By default says data is always valid.
        See _validata_data() for more information.
        """
        return True

    @implemented_by_keychain
    def _init_data(self):
        """Initialize data field.

        How to use:
        - Create a method prefixed with your namespace
        - Return a dict with the keys and may be default
        values your expect.

        This method will be called on write().

        Example:
        def _hereismynamspace_init_data():
            return { 'some_param': 'default_value' }

        @returns dict
        """
        pass

    def _default_init_data(self):
        """Default initialization.

        See _init_data() for more information.
        """
        return {}

    @staticmethod
    def _retrieve_env():
        """Return the current environments.

        You may override this function to fit your needs.

        returns: a tuple like:
            ('dev', 'test', False)
            Which means accounts for dev, test and blank (not set)
            Order is important: the first one is used for encryption.
        """
        current = config.get('running_env') or False
        envs = [current]
        if False not in envs:
            envs.append(False)
        return envs

    @staticmethod
    def _serialize_data(data):
        return json.dumps(data)

    @staticmethod
    def _parse_data(data):
        try:
            return json.loads(data)
        except ValueError:
            raise ValidationError(_("Data should be a valid JSON"))

    @classmethod
    def _encode_password(cls, data, env):
        cipher = cls._get_cipher(env)
        return cipher.encrypt(str((data or '').encode('UTF-8')))

    @classmethod
    def _decode_password(cls, data):
        cipher = cls._get_cipher()
        try:
            return unicode(cipher.decrypt(str(data)), 'UTF-8')
        except InvalidToken:
            raise UserError(_(
                "Password has been encrypted with a different "
                "key. Unless you can recover the previous key, "
                "this password is unreadable."
            ))

    @classmethod
    def _get_cipher(cls, force_env=None):
        """Return a cipher using the keys of environments.

        force_env = name of the env key.
        Useful for encoding against one precise env
        """
        def _get_keys(envs):
            suffixes = [
                '_%s' % env if env else ''
                for env in envs]  # ('_dev', '')
            keys_name = [
                'keychain_key%s' % suf
                for suf in suffixes]  # prefix it
            keys_str = [
                config.get(key)
                for key in keys_name]  # fetch from config
            return [
                Fernet(key) for key in keys_str  # build Fernet object
                if key and len(key) > 0  # remove False values
            ]

        if force_env:
            envs = [force_env]
        else:
            envs = cls._retrieve_env()  # ex: ('dev', False)
        keys = _get_keys(envs)
        if len(keys) == 0:
            raise UserError(_(
                "No 'keychain_key_%s' entries found in config file. "
                "Use a key similar to: %s" % (envs[0], Fernet.generate_key())
            ))
        return MultiFernet(keys)
