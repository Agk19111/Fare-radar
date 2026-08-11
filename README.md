# Fare Radar v3

A personalized, no-AI-quota flight-deal dashboard and email-alert bot.

## What changed from v2

The old page called an AI model directly from browser JavaScript. That made the scan depend on a chat/API usage window, exposed the architecture to CORS/authentication problems, and could never safely protect an API key. Version 3 moves all scheduled work into GitHub Actions.

The backend:

1. Reads public deal-site RSS feeds every three hours.
2. Filters posts for YYJ, YVR, YXX, BLI, SEA, YYC, YEG and unusually cheap YYZ deals.
3. Writes `deals.json` for the dashboard.
4. Emails newly matched posts.
5. Deploys the updated page to GitHub Pages.

The page itself only loads `deals.json`, so pressing **Refresh feed** does not use Claude, ChatGPT or another model.
