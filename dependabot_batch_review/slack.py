import requests


class SlackClient:
    """
    Client for posting messages to Slack.
    """

    def __init__(self, token: str):
        self.token = token

    def post_message(self, channel: str, message: str):
        """
        Post a message to a Slack channel.

        See https://api.slack.com/methods/chat.postMessage.
        """

        body = {
            "channel": channel,
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
