import json
import logging
import os
import signal
import sys
import time
import re
import importlib
from zerver.lib.actions import internal_send_message
from zerver.models import UserProfile, \
    get_bot_state, set_bot_state, get_bot_state_size, is_key_in_bot_state
from zerver.lib.integrations import EMBEDDED_BOTS

from six.moves import configparser

if False:
    from mypy_extensions import NoReturn
from typing import Any, Optional, List, Dict, Text
from types import ModuleType

our_dir = os.path.dirname(os.path.abspath(__file__))

from zulip_bots.lib import RateLimit

def get_bot_handler(service_name):
    # type: (str) -> Any

    # Check that this service is present in EMBEDDED_BOTS, add exception handling.
    is_present_in_registry = any(service_name == embedded_bot_service.name for embedded_bot_service in EMBEDDED_BOTS)
    if not is_present_in_registry:
        return None
    bot_module_name = 'zulip_bots.bots.%s.%s' % (service_name, service_name)
    bot_module = importlib.import_module(bot_module_name)  # type: Any
    return bot_module.handler_class()

class StateHandlerError(Exception):
    pass

class StateHandler(object):
    state_size_limit = 10000000   # type: int # TODO: Store this in the server configuration model.

    def __init__(self, user_profile):
        # type: (UserProfile) -> None
        self.user_profile = user_profile
        self.marshal = lambda obj: json.dumps(obj)
        self.demarshal = lambda obj: json.loads(obj)

    def get(self, key):
        # type: (Text) -> Text
        return self.demarshal(get_bot_state(self.user_profile, key))

    def put(self, key, value):
        # type: (Text, Text) -> None
        old_entry_size = get_bot_state_size(self.user_profile, key)
        new_entry_size = len(key) + len(value)
        old_state_size = get_bot_state_size(self.user_profile)
        new_state_size = old_state_size + (new_entry_size - old_entry_size)
        if new_state_size > self.state_size_limit:
            raise StateHandlerError("Cannot set state. Request would require {} bytes storage. "
                                    "The current storage limit is {}.".format(new_state_size, self.state_size_limit))
        elif type(key) is not str:
            raise StateHandlerError("Cannot set state. The key type is {}, but it should be str.".format(type(key)))
        else:
            marshaled_value = self.marshal(value)
            if type(marshaled_value) is not str:
                raise StateHandlerError("Cannot set state. The value type is {}, but it "
                                        "should be str.".format(type(marshaled_value)))
            set_bot_state(self.user_profile, key, marshaled_value)

    def contains(self, key):
        # type: (Text) -> bool
        return is_key_in_bot_state(self.user_profile, key)

class EmbeddedBotHandler(object):
    def __init__(self, user_profile):
        # type: (UserProfile) -> None
        # Only expose a subset of our UserProfile's functionality
        self.user_profile = user_profile
        self._rate_limit = RateLimit(20, 5)
        self.full_name = user_profile.full_name
        self.email = user_profile.email
        self.storage = StateHandler(user_profile)

    def send_message(self, message):
        # type: (Dict[str, Any]) -> None
        if self._rate_limit.is_legal():
            recipients = message['to'] if message['type'] == 'stream' else ','.join(message['to'])
            internal_send_message(realm=self.user_profile.realm, sender_email=self.user_profile.email,
                                  recipient_type_name=message['type'], recipients=recipients,
                                  subject=message.get('subject', None), content=message['content'])
        else:
            self._rate_limit.show_error_and_exit()

    def send_reply(self, message, response):
        # type: (Dict[str, Any], str) -> None
        if message['type'] == 'private':
            self.send_message(dict(
                type='private',
                to=[x['email'] for x in message['display_recipient']],
                content=response,
                sender_email=message['sender_email'],
            ))
        else:
            self.send_message(dict(
                type='stream',
                to=message['display_recipient'],
                subject=message['subject'],
                content=response,
                sender_email=message['sender_email'],
            ))

    def get_config_info(self, bot_name, section=None):
        # type: (str, Optional[str]) -> Dict[str, Any]
        conf_file_path = os.path.realpath(os.path.join(
            our_dir, '..', 'bots', bot_name, bot_name + '.conf'))
        section = section or bot_name
        config = configparser.ConfigParser()
        config.readfp(open(conf_file_path))  # type: ignore # likely typeshed issue
        return dict(config.items(section))
