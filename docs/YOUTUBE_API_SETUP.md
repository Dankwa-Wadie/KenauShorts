# YouTube Data API & OAuth Setup Guide

To let KenauShorts upload rendered videos directly to your YouTube channel as Shorts, you will need to create a free OAuth 2.0 client in Google Cloud Console.

> **Not the same as your Gemini API key.** The AI provider keys (Gemini/Claude/OpenAI) in the Connections tab are simple pasted keys from [Google AI Studio](https://aistudio.google.com/app/apikey) and similar. YouTube uploading is different: Google requires your explicit, one-time consent for anything that can post to your channel, so there is no pasteable-key option for it at all — only the OAuth flow below, which ends with a real Google sign-in window opening in your browser. That's expected, not an error.

---

## Step 1: Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Click the project dropdown in the top-left and select **New Project**.
3. Name your project (e.g. `KenauShorts-Uploader`) and click **Create**.

---

## Step 2: Enable the YouTube Data API v3

1. In the left sidebar, go to **APIs & Services**.
2. Click **Enabled APIs & services**.
3. Click **+ Enable APIs and Services** at the top — this opens the API Library.
4. Search for **YouTube Data API v3**, click it, then click **Enable**.
5. Click **Manage** on the page that appears after enabling it.

---

## Step 3: Create a Desktop OAuth Client

1. In the left sidebar, go to **Credentials**.
2. Click **+ Create Credentials** → **OAuth client ID**.
   - If this is the first OAuth client in the project, Google will first ask you to configure the app's audience/branding (app name, support email) — fill that in when prompted and continue; it doesn't need anything beyond the basics for personal use.
3. Set **Application type** to **Desktop app** *(important — do not choose "Web application")*.
4. Give it a name (e.g. `KenauShorts Desktop Client`) and click **Create**.
5. Click **Download JSON** on the confirmation dialog (or find it later under **Credentials** → **Clients** → your client → the download icon).
6. Rename the downloaded file to `client_secret.json` and save it directly in your `KenauShorts/` root folder.

> If Google puts your project's OAuth consent in **Testing** mode, only Google accounts you've explicitly added as test users can complete sign-in — add the Gmail address of the channel you're uploading to under **Audience** → **Test users** if you hit an access-blocked screen in Step 4 below.

---

## Step 4: Authorize Your Channel

In the KenauShorts Studio web interface:
1. Navigate to the **Connections** tab.
2. Click **🔑 Connect / Authorize YouTube**.
3. A Google sign-in window will open in your browser.
4. Sign in with your YouTube channel account and click **Allow**.
5. Once complete, KenauShorts will save `token.json` and your status will change to **Connected & Ready**!
