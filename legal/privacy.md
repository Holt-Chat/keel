# Privacy Policy

_Last updated: 2026/06/22_

This Privacy Policy explains what this Holt instance handles and how. Holt is open source and end-to-end encrypted. This is a self-hosted instance, so the operator of the server ("the operator") is the data controller. These defaults ship with the Holt software and may be edited by the operator.

## What stays private

Holt is built so the server learns as little as possible about your conversations.

- **Message contents and files** are sealed with a per-channel AES key. That AES key is wrapped to each member's RSA public key, and it is rotated when members join or leave, so old keys cannot read new messages (forward secrecy). **The server only stores ciphertext it cannot read.**
- **Your private key** is generated in your browser and saved to a key file you keep. It never leaves your device and is never sent to the server.

## What the server stores

To run the service, the instance stores:

- Your **username**, optional **display name**, and **profile picture**.
- Your **public key** and password-hashed credentials, so you can log in.
- **Encrypted message content, attachments, and the wrapped channel keys** (ciphertext only).
- **Channel membership** and basic metadata such as timestamps and message ordering.
- **Session information** (device and browser labels) for the sessions you have open, so you can review and revoke them.

## Presence, typing, and last seen

To make chat feel live, Holt shares some real-time signals with people who can see you:

- **Presence** (online, idle, do-not-disturb) and **typing** indicators.
- **Last seen** time when you are offline.

You control these:

- Set your status to **invisible** and your presence and typing stop being shared.
- Turn off **Share last seen** to stop sharing your last-seen time.
- Turn off **Share typing** to stop sending typing indicators.

These opt-outs are honored by the server. Presence and typing can also be disabled instance-wide by the operator.

## What the server does not do

- It cannot read the contents of your encrypted messages or files.
- It does not sell your data.
- It includes no third-party advertising or tracking by default.

## Retention and deletion

- Encrypted content stays until the message, channel, or account is deleted.
- Deleting your account removes your user record and the channels you solely own, and frees the files that are no longer referenced.

## Your choices

- You can edit your profile, review and revoke sessions, block users, and delete your account at any time from the app.
- Because the server holds only ciphertext, the operator cannot restore message contents for you if you lose your key file.

## Contact

For privacy questions, contact the operator of this instance.
