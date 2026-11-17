# Privacy Policy — autoclip

Last updated: 17 November 2026

autoclip is a personal command-line tool run only by its owner on his own computer. It renders short
vertical videos and uploads them to the owner's own YouTube channel (Light Moments) using the
YouTube Data API.

## What it accesses
- Only the owner's own YouTube account, through Google OAuth with the `youtube.upload` scope.
- It does not access any other user's account, channel or data.

## What it stores
- An OAuth token (`token.json`), saved locally on the owner's computer and used only to upload videos.
- No personal data about any other person is collected, stored, sold or shared.

## Third parties
- Videos and their titles and descriptions are sent only to YouTube.
- autoclip uses YouTube API Services, so the YouTube Terms of Service (https://www.youtube.com/t/terms)
  and the Google Privacy Policy (https://policies.google.com/privacy) also apply.

## YouTube API Services
- autoclip uses YouTube API Services (YouTube Data API v3, `videos.insert`).
- By using autoclip you agree to the YouTube Terms of Service: https://www.youtube.com/t/terms
- Google Privacy Policy: https://policies.google.com/privacy

## Data retention and deletion
- The only data autoclip keeps is the OAuth token in `token.json` on the owner's computer.
  It is kept until the owner deletes the file or revokes access, and is never sent anywhere except Google.
- To delete it: delete `token.json`, and revoke access at https://myaccount.google.com/permissions
  (Google's security settings page). After revoking, autoclip can no longer upload anything.
- autoclip stores no API data (no video data, statistics or user information) from YouTube.
- Videos already uploaded live on the owner's YouTube channel and can be deleted at any time in YouTube Studio.

## Revoking access
- Access can be removed at any time at https://myaccount.google.com/permissions

## Contact
- Open an issue in this repository.
