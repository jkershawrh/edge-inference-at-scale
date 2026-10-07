# Discord testing transport

Discord is an internet-connected development adapter for exercising the same
RAG and LLM pipeline that SMS, GSM, and LoRa transports will use. It is not part
of the disconnected field runtime.

The adapter uses a signed `/ask` slash command. It does not read ordinary
channel messages and does not require Discord's privileged message-content
intent. Requests are acknowledged immediately, processed asynchronously, and
then written into the deferred Discord response.

## 1. Create the Discord application

1. Create an application in the Discord Developer Portal.
2. Copy its **Public Key**. This key is safe to deploy; it is not the bot token.
3. Install the application in a private test server with the
   `applications.commands` scope.
4. Keep the bot token in your local shell only. Do not store it in Git or an
   OpenShift ConfigMap.

## 2. Deploy the public key

For local Compose testing:

```bash
export DISCORD_PUBLIC_KEY="<application-public-key>"
docker compose up
```

For OpenShift:

```bash
helm upgrade --install edge-inference chart/ \
  --set-string discord.publicKey="<application-public-key>"
```

The chart's TLS route exposes the interaction endpoint at:

```text
https://<api-gateway-route>/discord/interactions
```

Enter that URL as the application's **Interactions Endpoint URL**. Discord will
send a signed PING; the service validates the Ed25519 signature before replying.

## 3. Register `/ask`

Guild-scoped commands update immediately and are best for development:

```bash
export DISCORD_APPLICATION_ID="<application-id>"
export DISCORD_BOT_TOKEN="<temporary-local-token>"
export DISCORD_GUILD_ID="<private-test-server-id>"
./scripts/register_discord_command.py
unset DISCORD_BOT_TOKEN
```

Omit `DISCORD_GUILD_ID` to register globally. Global command propagation can be
slower.

## 4. Exercise the pipeline

In the private test server, run:

```text
/ask question: What is the nearest emergency shelter?
```

The API gateway acknowledges the interaction within Discord's three-second
window, routes a `channel=discord` envelope through privacy filtering, RAG, and
the configured model, and edits the deferred response. Replies are ephemeral by
default; set `DISCORD_EPHEMERAL=false` only when shared test output is desired.

## Security and scope

- Every interaction must carry a valid Discord Ed25519 signature.
- Discord interaction tokens are used only for the deferred reply and are never
  placed in the shared message envelope or logs.
- `allowed_mentions` is empty on replies, preventing retrieved or generated text
  from pinging users and roles.
- The bot token is needed only when registering the command.
- Discord availability says nothing about GSM, LoRa, solar, or disconnected
  performance; it validates conversation behavior and the application pipeline.
