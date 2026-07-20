import { db } from 'sdk';
import { sessions } from 'schema';
import { eq } from 'sdk/db';

export async function getSession(chatId) {
  return await db.select().from(sessions).where(eq(sessions.chatId, chatId)).get();
}

export async function setSession(chatId, state, payload = null) {
  await db
    .insert(sessions)
    .values({ chatId, state, payload })
    .onConflictDoUpdate({ target: sessions.chatId, set: { state, payload } })
    .run();
}

export async function clearSession(chatId) {
  await db.delete(sessions).where(eq(sessions.chatId, chatId)).run();
}
