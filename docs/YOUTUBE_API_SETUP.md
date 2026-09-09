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

1. In the left navigation menu, go to **APIs & Services** → **Library**.
2. Search for **YouTube Data API v3**.
3. Click on it and click **Enable**.

---

## Step 3: Configure the OAuth Consent Screen

1. Go to **APIs & Services** → **OAuth consent screen**.
2. Select **External** and click **Create**.
3. Fill in the required fields:
   - **App name**: `KenauShorts`
   - **User support email**: Your personal email
   - **Developer contact information**: Your personal email
4. Click **Save and Continue**.
5. Under **Scopes**, click **Add or Remove Scopes**:
   - Filter and select: `https://www.googleapis.com/auth/youtube.upload`
   - Click **Update** and then **Save and Continue**.
6. Under **Test users**:
   - Click **Add Users** and enter the Gmail address that owns the YouTube channel you want to upload to.
   - Click **Save and Continue**.

---

## Step 4: Create Desktop Client ID

1. Go to **APIs & Services** → **Credentials**.
2. Click **+ Create Credentials** → **OAuth client ID**.
3. Set **Application type** to:
   **Desktop app** *(Important: Do NOT select "Web application")*.
4. Name it `KenauShorts Desktop Client` and click **Create**.
5. In the dialog that appears, click **Download JSON**.
6. Rename the downloaded file to `client_secret.json` and save it directly in your `KenauShorts/` root folder.

---

## Step 5: Authorize Your Channel

In the KenauShorts Studio web interface:
1. Navigate to the **Connections** tab.
2. Click **🔑 Connect / Authorize YouTube**.
3. A Google sign-in window will open in your browser.
4. Sign in with your YouTube channel account and click **Allow**.
5. Once complete, KenauShorts will save `token.json` and your status will change to **Connected & Ready**!
