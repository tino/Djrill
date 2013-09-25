from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.message import sanitize_address, DEFAULT_ATTACHMENT_MIME_TYPE

# Oops: this file has the same name as our app, and cannot be renamed.
#from djrill import MANDRILL_API_URL, MandrillAPIError, NotSupportedByMandrillError
from ... import MANDRILL_API_URL, MandrillAPIError, NotSupportedByMandrillError

from base64 import b64encode
from email.mime.base import MIMEBase
from email.utils import parseaddr
import json
import mimetypes
import requests

DjrillBackendHTTPError = MandrillAPIError # Backwards-compat Djrill<=0.2.0


class DjrillBackend(BaseEmailBackend):
    """
    Mandrill API Email Backend
    """

    def __init__(self, **kwargs):
        """
        Set the API key, API url and set the action url.
        """
        super(DjrillBackend, self).__init__(**kwargs)
        self.api_key = getattr(settings, "MANDRILL_API_KEY", None)
        self.api_url = MANDRILL_API_URL

        if not self.api_key:
            raise ImproperlyConfigured("You have not set your mandrill api key "
                "in the settings.py file.")

        self.api_send = self.api_url + "/messages/send.json"
        self.api_send_template = self.api_url + "/messages/send-template.json"

    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        num_sent = 0
        for message in email_messages:
            sent = self._send(message)

            if sent:
                num_sent += 1

        return num_sent

    def _send(self, message):
        if not message.recipients():
            return False

        try:
            msg_dict = self._build_standard_message_dict(message)
            self._add_mandrill_options(message, msg_dict)
            if getattr(message, 'alternatives', None):
                self._add_alternatives(message, msg_dict)
            self._add_attachments(message, msg_dict)
        except NotSupportedByMandrillError:
            if not self.fail_silently:
                raise
            return False

        api_url = self.api_send
        api_params = {
            "key": self.api_key,
            "message": msg_dict
        }

        # check if template is set in message to send it via
        # api url: /messages/send-template.json
        if getattr(message, 'template_name', False):
            api_url = self.api_send_template
            api_params['template_name'] = message.template_name
            if hasattr(message, 'template_content'):
                api_params['template_content'] = \
                    self._expand_merge_vars(message.template_content)

        response = requests.post(api_url, data=json.dumps(api_params))

        if response.status_code != 200:
            if not self.fail_silently:
                raise MandrillAPIError(
                    status_code=response.status_code,
                    response=response,
                    log_message="Failed to send a message to %s, from %s" %
                                (msg_dict['to'], msg_dict['from_email']))
            return False
        return True

    def _build_standard_message_dict(self, message):
        """Create a Mandrill send message struct from a Django EmailMessage.

        Builds the standard dict that Django's send_mail and send_mass_mail
        use by default. Standard text email messages sent through Django will
        still work through Mandrill.

        Raises NotSupportedByMandrillError for any standard EmailMessage
        features that cannot be accurately communicated to Mandrill
        (e.g., prohibited headers).
        """
        sender = sanitize_address(message.from_email, message.encoding)
        from_name, from_email = parseaddr(sender)

        recipients = message.to + message.cc # message.recipients() w/o bcc
        parsed_rcpts = [parseaddr(sanitize_address(addr, message.encoding))
                        for addr in recipients]
        to_list = [{"email": to_email, "name": to_name}
                   for (to_name, to_email) in parsed_rcpts]

        msg_dict = {
            "text": message.body,
            "subject": message.subject,
            "from_email": from_email,
            "to": to_list
        }
        if from_name:
            msg_dict["from_name"] = from_name

        if len(message.bcc) == 1:
            bcc = message.bcc[0]
            _, bcc_addr = parseaddr(sanitize_address(bcc, message.encoding))
            msg_dict['bcc_address'] = bcc_addr
        elif len(message.bcc) > 1:
            raise NotSupportedByMandrillError(
                "Too many bcc addresses (%d) - Mandrill only allows one"
                % len(message.bcc))

        if message.extra_headers:
            for k in message.extra_headers.keys():
                if k != "Reply-To" and not k.startswith("X-"):
                    raise NotSupportedByMandrillError(
                        "Invalid message header '%s' - Mandrill "
                        "only allows Reply-To and X-* headers" % k)
            msg_dict["headers"] = message.extra_headers

        return msg_dict

    def _add_mandrill_options(self, message, msg_dict):
        """Extend msg_dict to include Mandrill options set on message"""
        # Mandrill attributes that can be copied directly:
        mandrill_attrs = [
            'from_name', # overrides display name parsed from from_email above
            'track_opens', 'track_clicks', 'auto_text', 'auto_html',
            'inline_css', 'url_strip_qs',
            'tracking_domain', 'signing_domain',
            'tags', 'preserve_recipients',
            'google_analytics_domains', 'google_analytics_campaign',
            'metadata']
        for attr in mandrill_attrs:
            if hasattr(message, attr):
                msg_dict[attr] = getattr(message, attr)

        # Allow simple python dicts in place of Mandrill
        # [{name:name, value:value},...] arrays...
        if hasattr(message, 'global_merge_vars'):
            msg_dict['global_merge_vars'] = \
                self._expand_merge_vars(message.global_merge_vars)
        if hasattr(message, 'merge_vars'):
            # For testing reproducibility, we sort the recipients
            msg_dict['merge_vars'] = [
                { 'rcpt': rcpt,
                  'vars': self._expand_merge_vars(message.merge_vars[rcpt]) }
                for rcpt in sorted(message.merge_vars.keys())
            ]
        if hasattr(message, 'recipient_metadata'):
            # For testing reproducibility, we sort the recipients
            msg_dict['recipient_metadata'] = [
                { 'rcpt': rcpt, 'values': message.recipient_metadata[rcpt] }
                for rcpt in sorted(message.recipient_metadata.keys())
            ]

    def _expand_merge_vars(self, vardict):
        """Convert a Python dict to an array of name-content used by Mandrill.

        { name: value, ... } --> [ {'name': name, 'content': value }, ... ]
        """
        # For testing reproducibility, we sort the keys
        return [{'name': name, 'content': vardict[name]}
                for name in sorted(vardict.keys())]

    def _add_alternatives(self, message, msg_dict):
        """
        There can be only one! ... alternative attachment, and it must be text/html.

        Since mandrill does not accept image attachments or anything other
        than HTML, the assumption is the only thing you are attaching is
        the HTML output for your email.
        """
        if len(message.alternatives) > 1:
            raise NotSupportedByMandrillError(
                "Too many alternatives attached to the message. "
                "Mandrill only accepts plain text and html emails.")

        (content, mimetype) = message.alternatives[0]
        if mimetype != 'text/html':
            raise NotSupportedByMandrillError(
                "Invalid alternative mimetype '%s'. "
                "Mandrill only accepts plain text and html emails."
                % mimetype)

        msg_dict['html'] = content

    def _add_attachments(self, message, msg_dict):
        """Extend msg_dict to include any attachments in message"""
        if message.attachments:
            str_encoding = message.encoding or settings.DEFAULT_CHARSET
            mandrill_attachments = []
            mandrill_embedded_images = []
            for attachment in message.attachments:
                att_dict, is_embedded = self._make_mandrill_attachment(attachment, str_encoding)
                if is_embedded:
                    mandrill_embedded_images.append(att_dict)
                else:
                    mandrill_attachments.append(att_dict)
            if len(mandrill_attachments) > 0:
                msg_dict['attachments'] = mandrill_attachments
            if len(mandrill_embedded_images) > 0:
                msg_dict['images'] = mandrill_embedded_images

    def _make_mandrill_attachment(self, attachment, str_encoding=None):
        """Returns EmailMessage.attachments item formatted for sending with Mandrill.

        Returns mandrill_dict, is_embedded_image:
        mandrill_dict: {"type":..., "name":..., "content":...}
        is_embedded_image: True if the attachment should instead be handled as an inline image.

        """
        # Note that an attachment can be either a tuple of (filename, content,
        # mimetype) or a MIMEBase object. (Also, both filename and mimetype may
        # be missing.)
        is_embedded_image = False
        if isinstance(attachment, MIMEBase):
            name = attachment.get_filename()
            content = attachment.get_payload(decode=True)
            mimetype = attachment.get_content_type()
            # Treat image attachments that have content ids as embedded:
            if attachment.get_content_maintype() == "image" and attachment["Content-ID"] is not None:
                is_embedded_image = True
                name = attachment["Content-ID"]
        else:
            (name, content, mimetype) = attachment

        # Guess missing mimetype from filename, borrowed from
        # django.core.mail.EmailMessage._create_attachment()
        if mimetype is None and name is not None:
            mimetype, _ = mimetypes.guess_type(name)
        if mimetype is None:
            mimetype = DEFAULT_ATTACHMENT_MIME_TYPE

        try:
            content_b64 = b64encode(content)
        except TypeError:
            # Python 3 b64encode requires bytes. Convert str attachment:
            if isinstance(content, str):
                content_bytes = content.encode(str_encoding)
                content_b64 = b64encode(content_bytes)
            else:
                raise

        mandrill_attachment = {
            'type': mimetype,
            'name': name or "",
            'content': content_b64.decode('ascii'),
        }
        return mandrill_attachment, is_embedded_image

