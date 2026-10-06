# Campaign Scout

Watches your Discord 24/7 and sorts every campaign onto your **UGC Kickstarter Client Roadmap** Miro board into three columns: **🟢 GREAT**, **🟡 GOOD**, **🔴 BAD**.

Each card shows:
- the campaign
- the CPM and/or retainer
- the pay details
- the platforms
- the campaign managers
- where to join
- a link back to the original Discord post

```
Campaign servers ──(Follow)──▶ #campaign-feed in your server ──▶ bot ──▶ Claude reads it ──▶ your cutoffs ──▶ Miro card
```

Setup takes about 20 minutes, once.

---

## 1. Make your feed server (2 min)

In Discord, create a private server, for example **Campaign Feed**, with one text channel, **#campaign-feed**. Everything the bot reads lands here.

## 2. Create the bot (5 min)

1. Go to **discord.com/developers/applications**, click **New Application**, and name it `Campaign Scout`.
2. In the left menu, click **Bot**, then **Reset Token**, and copy the token. This is your `DISCORD_TOKEN`.
3. On the same page, under **Privileged Gateway Intents**, turn on **Message Content Intent**, then click **Save**.
4. In the left menu, click **OAuth2**, then **URL Generator**:
   - Under scopes, tick **bot**.
   - Under Bot Permissions, tick **View Channels** and **Read Message History**.
   - Copy the URL at the bottom, open it, pick **Campaign Feed**, and click **Authorize**.

## 3. Pipe the campaign servers in (1 min per server)

In each campaign server, open the channel where campaigns get announced. It has a **megaphone** icon.

1. Click **Follow** at the top of the channel. You can also right-click the channel and choose **Follow**.
2. Pick **Campaign Feed**, then **#campaign-feed**.

From then on, every new post there gets copied into your feed automatically.

**Campaign in a regular channel?** Those can't be followed. Hover the message, click **Forward**, and send it to **#campaign-feed**. The bot treats it the same way.

## 4. Miro token (3 min)

1. In Miro, click your profile picture and choose **Settings**, then open **Your apps** and click **Create new app**. Name it `Campaign Scout`.
2. Pick the **team your UGC Kickstarter board is in**.
3. Under **Permissions**, tick `boards:read` and `boards:write`.
4. Click **Install app and get OAuth token**, then pick the same team.
5. Copy the token. This is your `MIRO_TOKEN`.

The board ID is already filled in for you: `uXjVHhVoW2M=`.

## 5. Claude API key (2 min)

1. Go to **console.anthropic.com**, open **API Keys**, and click **Create Key**. This is your `ANTHROPIC_API_KEY`.
2. Add a few dollars of credit under **Billing**.

The bot uses Claude Haiku, so each post costs a fraction of a cent.

## 6. Put it on Railway so it runs 24/7 (7 min)

1. Create the repo on GitHub:
   - Go to **github.com/new**, name the repo `campaign-scout`, set it to **Private**, and click **Create**.
   - Click **uploading an existing file**, drag in every file from this folder, and commit.
2. Deploy it on Railway:
   - Go to **railway.com** and sign in with GitHub.
   - Click **New Project**, choose **Deploy from GitHub repo**, and pick `campaign-scout`.
3. Add your settings:
   - Open the service, go to **Variables**, and click **Raw Editor**.
   - Paste the contents of `.env.example` with your values filled in, then click **Update**.
   - Railway redeploys on its own.
4. Open **Deployments**, then **View Logs**. You should see:
   ```
   Logged in as Campaign Scout#1234
   Created frame 🟢 GREAT Campaigns   (first run only)
   Miro board ready: ...
   Watching 1 channel(s): #campaign-feed
   ```
5. Railway's trial runs out, so switch to the **Hobby** plan to keep it on 24/7.

The three frames appear on your board to the right of everything that's already there.

---

## Settings

| Variable | What it does |
|---|---|
| `GREAT_CPM` / `GOOD_CPM` | $ per 1,000 views needed for Great / Good. Anything below Good is Bad. |
| `GREAT_RETAINER` / `GOOD_RETAINER` | $ per month needed for Great / Good. |
| `NO_PAY_TIER` | Where campaigns with no $ listed go (default `Bad`). |
| `WATCH_CHANNEL_IDS` | Only watch these channels. Blank means every channel in your feed server. |
| `BACKFILL_HOURS` | On restart, catch up on this many hours of posts (default 24). |

A campaign lands in the **best** tier either its CPM or its retainer earns. For example, a $0.50 CPM with a $2,000/mo retainer can still be Great.

Change any cutoff in Railway's **Variables** whenever you want, and it applies to new campaigns.

## Good to know

- **Duplicates are skipped.** The same post, or the same campaign name posted again, won't make a second card.
- **Text only.** If a campaign is posted only as an image with no text, the bot can't read the numbers. Forward it with a quick note like "Brand X, $1.50 CPM, DM @manager".
- **You can still edit the board.** Drag cards or add notes freely. New cards slot into the next open spot in their column, and full columns grow taller on their own.
- **Run it on your own computer instead:**
  1. Run `pip install -r requirements.txt`.
  2. Copy `.env.example` to `.env` and fill it in.
  3. Run `python bot.py`.

  It only watches while the computer is on.
