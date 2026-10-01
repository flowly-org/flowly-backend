// Cloudflare Email Worker: hands mail for <token>@in.flowly.net to Flowly, which turns it into a task.
// Set up: see README.md next to this file.
import PostalMime from 'postal-mime';

export default {
  async email(message, env) {
    const mail = await PostalMime.parse(message.raw);
    const text = mail.text || (mail.html || '').replace(/<[^>]+>/g, ' ').replace(/\s+\n/g, '\n');
    const res = await fetch(env.FLOWLY_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Flowly-Key': env.FLOWLY_KEY },
      body: JSON.stringify({
        to: message.to,
        from: message.from,
        subject: mail.subject || '',
        text,
        message_id: mail.messageId || '',
      }),
    });
    if (res.status === 404) {
      message.setReject('No Flowly inbox at this address.');
    } else if (!res.ok) {
      // Thrown errors make the sending server retry later.
      throw new Error(`Flowly answered ${res.status}`);
    }
  },
};
