from __future__ import annotations
import json
import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, TypedDict, Tuple, Union
from urllib.parse import urlparse

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from sentry.http import safe_urlopen
from sentry.plugins.bases import notify
from sentry.utils.safe import safe_execute
from sentry.utils.strings import truncatechars

from . import __doc__ as package_doc
from . import __version__

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
EVENT_TITLE_MAX_LENGTH = 500

logger = logging.getLogger("sentry.plugins.sentry_telegram_plus")


class ChannelFilter(TypedDict):
    type: str
    value: str

class FilterGroup(TypedDict, total=False):
    and_filters: List[Union[ChannelFilter, FilterGroup]]
    or_filters: List[Union[ChannelFilter, FilterGroup]]


class ChannelConfig(TypedDict, total=False):
    api_token: str
    receivers: str
    template: Optional[str]
    api_origin: Optional[str]
    filters: Union[List[ChannelFilter], FilterGroup]


class ChannelsConfigJson(TypedDict):
    api_origin: Optional[str]
    channels: List[ChannelConfig]


def validate_api_origin(value: str, **kwargs):
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ValidationError(
            _("Telegram API origin must be a valid URL starting with http:// or https://.")
        )
    return value


def validate_channels_config_json(value: str, **kwargs):
    if not value:
        return value

    try:
        json.loads(value)
    except json.JSONDecodeError:
        raise ValidationError(_("Invalid JSON format. Please check for syntax errors."))
    except (TypeError, ValueError) as e:
        raise ValidationError(_(f"Invalid JSON data: {e}"))
    return value

class TelegramNotificationsOptionsForm(notify.NotificationConfigurationForm):
    api_origin = forms.CharField(
        label=_("Telegram API origin"),
        widget=forms.TextInput(attrs={"placeholder": "https://api.telegram.org"}),
        initial="https://api.telegram.org",
        help_text=_(
            "The base URL for the Telegram Bot API. Defaults to https://api.telegram.org."
        ),
    )
    channels_config_json = forms.CharField(
        label=_("Channels Configuration (JSON)"),
        widget=forms.Textarea(attrs={"class": "span10", "rows": 15}),
        help_text=_(
            "JSON configuration for routing messages to different channels. "
            "Each channel can have its own API token, receivers, message template, and filters. "
            "If no filters are specified for a channel, it acts as a default fallback. "
            "Example: <pre>{&quot;api_origin&quot;: &quot;https://api.telegram.org&quot;, &quot;channels&quot;: [{&quot;api_token&quot;: &quot;YOUR_BOT_TOKEN&quot;, &quot;receivers&quot;: &quot;-123456789;2&quot;, &quot;template&quot;: &quot;&quot;, &quot;filters&quot;: [{&quot;type&quot;:&quot;regex__message&quot;, &quot;value&quot;: &quot;.*error.*&quot;}]}]}</pre>"
        ),
        required=True,
    )
    default_message_template = forms.CharField(
        label=_("Default Message Template"),
        widget=forms.Textarea(attrs={"class": "span4"}),
        help_text=_(
            "Set in standard Python's {}-format convention. "
            "Available names / macroses are: {project_name}, {url}, {title}, {message}, {tag[%your_tag%], short_id, times_seen, platform, event_datetime}. "
            "Undefined tags will be shown as [NA]. This template is used if a specific channel template is empty."
        ),
        initial="*[Sentry]* {project_name} {tag[level]}: *{title}*\n```\n{message}```\n{url}",
        required=True,
    )


class TelegramNotificationsPlugin(notify.NotificationPlugin):
    title = "Telegram Notifications Plus"
    slug = "sentry_telegram_plus"
    description = package_doc
    version = __version__
    author = "Boris Savinov"
    author_url = "https://gitlab.hellodoc.team/hellodoc/sentry-telegram-plus"
    resource_links = [
        ("Original version", "https://github.com/butorov/sentry-telegram"),
        (
            "Hello, Doc Repo",
            "https://gitlab.hellodoc.team/hellodoc/sentry-telegram-plus",
        ),
    ]

    conf_key = "sentry_telegram_plus"
    conf_title = title

    project_conf_form = TelegramNotificationsOptionsForm

    def __init__(self):
        super().__init__()
        self._regex_cache: Dict[str, re.Pattern] = {}

    def is_configured(self, project, **kwargs) -> bool:
        """Проверяет, настроен ли плагин для проекта."""
        return bool(self.get_option('api_origin', project) and self.get_option('channels_config_json', project))

    def get_config(self, project, **kwargs) -> List[Dict[str, Any]]:
        """
        Возвращает конфигурацию полей для UI Sentry.
        """
        return [
            {
                'name': 'api_origin',
                'label': _('Telegram API origin'),
                'type': 'text',
                'placeholder': 'https://api.telegram.org',
                'validators': [validate_api_origin],
                'required': True,
                'default': 'https://api.telegram.org',
                'help': _('The base URL for the Telegram Bot API. Defaults to https://api.telegram.org.')
            },
            {
                'name': 'channels_config_json',
                'label': _('Channels Configuration (JSON)'),
                'type': 'textarea',
                'help': _(
                    'JSON configuration for routing messages to different channels. '
                    'Each channel can have its own API token, receivers, message template, and filters. '
                    'If no filters are specified for a channel, it acts as a default fallback. '
                    'Example: <pre>{&quot;api_origin&quot;: &quot;https://api.telegram.org&quot;, &quot;channels&quot;: [{&quot;api_token&quot;: &quot;YOUR_BOT_TOKEN&quot;, &quot;receivers&quot;: &quot;-123456789;2&quot;, &quot;template&quot;: &quot;&quot;, &quot;filters&quot;: [{&quot;type&quot;:&quot;regex__message&quot;, &quot;value&quot;: &quot;.*error.*&quot;}]}]}</pre>'
                ),
                'validators': [validate_channels_config_json],
                'required': True,
            },
            {
                'name': 'default_message_template',
                'label': _('Default Message Template'),
                'type': 'textarea',
                'help': _('Set in standard Python\'s {}-format convention. '
                          'Available names / macroses are: {project_name}, {url}, {title}, {message}, {tag[%your_tag%], short_id, times_seen, platform, event_datetime}. '
                          'Undefined tags will be shown as [NA]. This template is used if a specific channel template is empty.'),
                'validators': [],
                'required': True,
                'default': '*[Sentry]* {project_name} {tag[level]}: *{title}*\n```\n{message}```\n{url}'
            },
        ]

    def _escape_markdown_v1(self, text: str) -> str:
        """
        Экранирует специальные символы Markdown v1 для Telegram.
        Это необходимо, чтобы символы вроде *, _, `, [ отображались буквально,
        а не как форматирование, если они не предназначены для этого.
        """
        # Символы, которые нужно экранировать в Markdown v1
        # https://core.telegram.org/bots/api#markdown-style
        special_chars = r'_*`['
        escaped_text = "".join(['\\' + char if char in special_chars else char for char in text])
        return escaped_text

    def compile_message_text(
            self, message_template: str, message_params: Dict[str, Any], event_message: str
    ) -> str:
        """
        Собирает текст сообщения из шаблона и данных события, обрезая его по длине, если необходимо.
        """
        truncate_warning_text = "... (truncated)"

        try:
            max_message_body_len = TELEGRAM_MAX_MESSAGE_LENGTH - len(
                message_template.format(**message_params, message=truncate_warning_text)
            )
        except KeyError as e:
            missing_key = str(e).strip("'")
            logger.warning(
                f"Missing key '{missing_key}' in message parameters for template. "
            )
            temp_message_params = message_params.copy()
            temp_message_params[missing_key] = "-"

            max_message_body_len = TELEGRAM_MAX_MESSAGE_LENGTH - len(
                message_template.format(**temp_message_params, message=truncate_warning_text)
            )

        if max_message_body_len < 0:
            max_message_body_len = 0

        if len(event_message) > max_message_body_len:
            event_message = event_message[:max_message_body_len] + truncate_warning_text

        try:
            final_text = message_template.format(**message_params, message=event_message)
        except KeyError as e:
            missing_key = str(e).strip("'")
            logger.warning(
                f"Missing key '{missing_key}' in message parameters for final template. "
                "Replacing with '-' and retrying final message formatting."
            )
            temp_message_params = message_params.copy()
            temp_message_params[missing_key] = "-"
            final_text = message_template.format(**temp_message_params, message=event_message)

        return final_text

    def build_message(self, group, event, message_template: str) -> Dict[str, Any]:
        """Создание сообщения для отправки в Telegram."""
        event_tags = defaultdict(lambda: "[NA]")
        event_tags.update({k: v for k, v in event.tags})

        escaped_title = self._escape_markdown_v1(truncatechars(event.title, EVENT_TITLE_MAX_LENGTH))
        escaped_event_message = self._escape_markdown_v1(event.message or "")

        message_params = {
            "title": escaped_title,
            "tag": event_tags,
            "project_name": group.project.name,
            "url": group.get_absolute_url(),
            "short_id": group.short_id,  # Короткий ID проблемы
            "times_seen": group.times_seen,  # Количество раз, сколько проблема произошла
            "platform": event.platform or "[NA]",  # Платформа
            "event_datetime": event.datetime or "[NA]",  # Время события
            "event_level": event_tags['level'],
        }
        text = self.compile_message_text(
            message_template,
            message_params,
            escaped_event_message,
        )

        return {
            "text": text,
            "parse_mode": "Markdown",
        }

    def build_url(self, api_origin: str, api_token: str) -> str:
        return f"{api_origin}/bot{api_token}/sendMessage"

    def _mask_url_token(self, url: str) -> str:
        """Маскирует API токен в URL для логирования."""
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.split('/')
        if len(path_parts) > 2 and path_parts[1] == 'bot':
            path_parts[2] = '...'  # Заменяем токен на троеточие
        masked_path = '/'.join(path_parts)
        return f"{parsed_url.scheme}://{parsed_url.netloc}{masked_path}{'?' + parsed_url.query if parsed_url.query else ''}"

    def get_receivers_list(self, receivers_str: str) -> List[List[str]]:
        """Парсит строку получателей в список списков [chat_id, message_thread_id]."""
        if not receivers_str:
            return []
        parsed_receivers: List[List[str]] = []
        for part in receivers_str.split(";"):
            stripped_part = part.strip()
            if stripped_part:
                parsed_receivers.append(stripped_part.split("/", maxsplit=1))
        return parsed_receivers

    def send_message(self, url: str, payload: Dict[str, Any], receiver: List[str]):
        """Отправляет сообщение одному получателю Telegram."""
        chat_id = receiver[0]
        payload_copy = payload.copy()
        payload_copy["chat_id"] = chat_id
        if len(receiver) > 1:
            payload_copy["message_thread_id"] = receiver[1]

        logger.debug("Sending message to %s" % receiver)
        try:
            response = safe_urlopen(
                method="POST",
                url=url,
                json=payload_copy,
            )
            response.raise_for_status()
            logger.debug(
                "Response code: %s, content: %s"
                % (response.status_code, response.content)
            )
        except Exception as e:
            logger.error(
                f"Failed to send message to chat_id {chat_id}: {e}", exc_info=True
            )

    def _get_compiled_regex(self, pattern: str) -> Optional[re.Pattern]:
        if pattern not in self._regex_cache:
            try:
                self._regex_cache[pattern] = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                logger.error(f"Invalid regex pattern '{pattern}': {e}")
                return None
        return self._regex_cache.get(pattern)

    def _search_in_json(self, data: Union[Dict, list, Any], regex_pattern: str) -> bool:
        """
        Рекурсивно ищет в словаре или списке совпадение с регулярным выражением.
        """
        pattern = self._get_compiled_regex(regex_pattern)
        if not pattern:
            return False

        def _recursive_search(obj: Any) -> bool:
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if isinstance(key, str) and pattern.search(key):
                        return True
                    if _recursive_search(value):
                        return True
            elif isinstance(obj, list):
                for item in obj:
                    if _recursive_search(item):
                        return True
            elif isinstance(obj, str):
                if pattern.search(obj):
                    return True
            return False

        return _recursive_search(data)


    def _match_filter(self, event: Any, filter_type: str, filter_value: str) -> bool:
        """Проверяет, соответствует ли событие заданному фильтру."""
        logger.info(f"_match_filter:\t type='{filter_type}', value='{filter_value}'")

        if filter_type == "regex__message":
            return self._check_regex_match(event.message or "", filter_value)
        elif filter_type == "regex__title":
            return self._check_regex_match(event.title or "", filter_value)
        elif filter_type.startswith("tag__"):
            tag_name = filter_type.split("__", 1)[1]
            tag_value = dict(event.tags).get(tag_name)
            return self._check_regex_match(tag_value, filter_value)
        elif filter_type == "level":
            return event.level == filter_value
        elif filter_type == "project_slug":
            return event.project and event.project.slug == filter_value
        elif filter_type == "value__tag":
            tags_dict = dict(event.tags)
            return filter_value in tags_dict.values()
        elif filter_type == "event_raw_regex":
            raw_data = event.get_raw_data()
            return self._search_in_json(raw_data, filter_value)
        logger.info(f"Unsupported filter: {filter_type}.")
        return False

    def _is_channel_filter(self, obj: Any) -> bool:
        """Проверяет, является ли объект простым ChannelFilter."""
        return isinstance(obj, dict) and "type" in obj

    def _is_filter_group(self, obj: Any) -> bool:
        """Проверяет, является ли объект группой фильтров (FilterGroup)."""
        return isinstance(obj, dict) and ("and_filters" in obj or "or_filters" in obj)

    def _is_empty_filter(self, filters: Any) -> bool:
        if filters is None:
            return True
        if isinstance(filters, list) and not filters:
            return True
        if isinstance(filters, dict):
            return not (filters.get("and_filters") or filters.get("or_filters"))
        return False

    def _check_filters_match(self, event: Any, filters: Any) -> bool:
        """
        Проверяет, соответствует ли событие заданным фильтрам.
        """
        if self._is_filter_group(filters):
            return self._evaluate_filter_group(event, filters)
        elif isinstance(filters, list):
            return all(
                self._match_filter(event, f["type"], f["value"])
                for f in filters
                if isinstance(f, dict) and f.get("type") and f.get("value")
            )
        else:
            logger.info(
                f"Incorrect 'filters' format (neither group nor list of filters): {type(filters)}. Returning False.")
            return False

    def _evaluate_single_filter_or_group(
            self, event: Any, sub_filter: Union[ChannelFilter, FilterGroup], depth: int
    ) -> Optional[bool]:
        if self._is_channel_filter(sub_filter):
            return self._match_filter(event, sub_filter["type"], sub_filter["value"])
        elif self._is_filter_group(sub_filter):
            return self._evaluate_filter_group(event, sub_filter, depth + 1)
        else:
            logger.info(f"{'  ' * depth}Incorrect filter type: {sub_filter}. Skipping this filter.")
            return None

    def _evaluate_filter_group(self, event: Any, filter_group: FilterGroup, depth: int = 0) -> bool:
        indent = "  " * depth
        logger.info(f"{indent}Evaluating filter group at depth {depth}: {filter_group}")
        if "and_filters" in filter_group and isinstance(filter_group["and_filters"], list):
            logger.info(f"{indent}  Processing AND filters:")
            for i, sub_filter in enumerate(filter_group["and_filters"]):
                match_result = self._evaluate_single_filter_or_group(event, sub_filter, depth + 1)
                # None также трактуем как несовпадение для AND
                if match_result is False or match_result is None:
                    return False
            # Все AND-фильтры совпали
            return True

        if "or_filters" in filter_group and isinstance(filter_group["or_filters"], list):
            logger.info(f"{indent}  Processing OR filters:")
            for i, sub_filter in enumerate(filter_group["or_filters"]):
                match_result = self._evaluate_single_filter_or_group(event, sub_filter, depth + 1)
                if match_result is True:
                    return True
                # Если match_result is False или None, продолжаем проверять другие OR-фильтры
            # Ни один OR-фильтр не совпал (или все были некорректны)
            return False

        logger.warning(f"{indent}No filters of type 'and_filters' / 'or_filters' for the group: {filter_group}. 🤔")
        # Если группа не содержит ни AND, ни OR фильтров, считаем, что она не совпадает.
        return False

    def _get_channels_config_data(self, project) -> Tuple[List[ChannelConfig], str]:
        """Получает и парсит конфигурацию каналов из настроек проекта."""
        config_json = self.get_option("channels_config_json", project)
        if not config_json:
            logger.info(f"channels_config_json is empty for project {project.slug}")
            return [], self.get_option("api_origin", project)

        try:
            config: ChannelsConfigJson = json.loads(config_json)

            if not isinstance(config, dict):
                logger.error(
                    f"Channels configuration for project {project.slug} must be a dictionary."
                )
                return [], self.get_option("api_origin", project)

            if "channels" not in config or not isinstance(config["channels"], list):
                logger.error(
                    f"Channels configuration for project {project.slug} must contain a 'channels' key with a list of channel objects."
                )
                return [], self.get_option("api_origin", project)
            if "api_origin" in config and not isinstance(config["api_origin"], str):
                logger.error(
                    f"The 'api_origin' in Channels Configuration for project {project.slug} must be a string."
                )
                return [], self.get_option("api_origin", project)

            return config.get("channels", []), config.get(
                "api_origin", self.get_option("api_origin", project)
            )
        except json.JSONDecodeError as e:
            logger.error(
                "Invalid JSON in channels_config_json for project %s: %s",
                project.slug, e, exc_info=True
            )
            return [], self.get_option("api_origin", project)
        except Exception as e:
            logger.error(
                f"Unexpected error loading channels config for project {project.slug}: {e}",
                exc_info=True,
            )
            return [], self.get_option("api_origin", project)

    def _get_matching_channels(self, event: Any, channels_config: List[ChannelConfig]) -> List[ChannelConfig]:
        """
        Определяет, какие каналы соответствуют событию на основе их фильтров.
        Возвращает список уникальных подходящих конфигураций каналов.
        """
        unique_matching_channels: Dict[str, ChannelConfig] = {}
        default_channel: Optional[ChannelConfig] = None

        for channel_config in channels_config:
            filters = channel_config.get("filters")
            channel_id = f"{channel_config.get('api_token')}|{channel_config.get('receivers')}"

            # Если фильтров нет (None, пустой список или пустой dict), это дефолтный канал.
            if self._is_empty_filter(filters):
                if default_channel is None:
                    default_channel = channel_config
                continue

            match_found = self._check_filters_match(event, filters)
            if match_found:
                unique_matching_channels[channel_id] = channel_config

        # Если не нашлось ни одного канала, соответствующего фильтрам,
        # и при этом есть дефолтный канал, используем его.
        if not unique_matching_channels and default_channel:
            default_channel_id = f"{default_channel.get('api_token')}|{default_channel.get('receivers')}"
            unique_matching_channels[default_channel_id] = default_channel
        return list(unique_matching_channels.values())

    def notify_users(self, group, event, fail_silently=False, **kwargs) -> None:
        """Отправка уведомлений."""
        logger.debug("Received notification for event: %s" % event)

        channels_config, global_api_origin = self._get_channels_config_data(
            group.project
        )
        default_template = self.get_option("default_message_template", group.project)

        if not channels_config:
            logger.info(
                "No Telegram channels configured for project %s. Event not sent.",
                group.project.slug,
            )
            return

        matching_channels = self._get_matching_channels(event, channels_config)

        if not matching_channels:
            logger.info(
                "No matching channels or default channel found for event in project %s. Event not sent.",
                group.project.slug,
            )
            return

        for channel_to_send in matching_channels:
            api_token = channel_to_send.get("api_token")
            receivers_str = channel_to_send.get("receivers")
            channel_template = channel_to_send.get("template") or default_template
            api_origin = channel_to_send.get("api_origin", global_api_origin)

            if not api_token or not receivers_str:
                logger.warning(
                    f"Channel missing api_token or receivers for project {group.project.slug}. Notification skipped for this channel."
                )
                continue

            receivers = self.get_receivers_list(receivers_str)
            if not receivers:
                logger.warning(
                    f"No valid receivers parsed for channel {receivers_str} in project {group.project.slug}. Notification skipped for this channel."
                )
                continue

            logger.debug(
                "Sending to receivers: %s for channel %s"
                % (", ".join(["/".join(item) for item in receivers] or ()), receivers_str)
            )

            payload = self.build_message(group, event, channel_template)

            url = self.build_url(api_origin, api_token)
            logger.info("Built URL for sending for channel %s: %s" % (receivers_str, self._mask_url_token(url)))

            for receiver in receivers:
                safe_execute(
                    self.send_message, url, payload, receiver, _with_transaction=False
                )