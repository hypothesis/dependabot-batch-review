import requests


class SlackClient:
    """
    Client for posting messages to Slack.
    """

    def __init__(self, token: str):
        self.token = token

    def post_message(self, channel_id: str, message: str):
        """
        Post a message to a Slack channel.

        See https://api.slack.com/methods/chat.postMessage.

        :param channel_id: Channel ID, available from the bottom of a channel's "About" dialog
        :param message: Message text using Slack's "mrkdwn" format
        """

        body = {
            "channel": channel_id,
            "text": message,
        }
        self._call(body)

    def _call(self, body):
        rsp = requests.post(
            "https://slack.com/api/chat.postMessage",
            json=body,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        rsp.raise_for_status()
        return rsp.json()
