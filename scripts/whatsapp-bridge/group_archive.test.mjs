import assert from 'node:assert/strict';
import test from 'node:test';
import { matchesInboundWhatsAppGroup, parseAllowedUsers } from './allowlist.js';
import { extractBridgeEvent } from './bridge_helpers.js';

const chatId = '120363000000000001@g.us';

test('explicit archive intake bypasses transport policy, not Python dispatch policy', () => {
  const args = { chatId, groupPolicy: 'pairing', groupAllowedUsers: parseAllowedUsers(''), sessionDir: '.' };
  assert.equal(matchesInboundWhatsAppGroup(args), false);
  assert.equal(matchesInboundWhatsAppGroup({ ...args, archiveGroupJids: [chatId] }), true);
  assert.equal(matchesInboundWhatsAppGroup({ ...args, archiveGroupJids: ['other@g.us'] }), false);
  assert.equal(matchesInboundWhatsAppGroup({ ...args, archiveGroupJids: ['*'] }), false);
});

test('archive-only extraction needs no media downloader', async () => {
  const event = await extractBridgeEvent({
    msg: { key: { id: 'one' }, messageTimestamp: 1790000000,
      message: { imageMessage: { caption: 'caption', mimetype: 'image/jpeg' } } },
    chatId, senderId: 'sender@lid', isGroup: true,
  });
  assert.equal(event.body, 'caption');
  assert.equal(event.hasMedia, true);
  assert.deepEqual(event.mediaUrls, []);
  assert.equal(event.timestamp, 1790000000);
});
