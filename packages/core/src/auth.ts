// Accounts (Module 16): name + password learners with a signed session cookie.
// - Passwords: scrypt (node:crypto) with a random salt; verified in constant time.
// - Session: "<learnerId>.<expiry s>.<HMAC-SHA256>" in an HttpOnly cookie; the secret is SESSION_SECRET,
//   else a random one generated once into data/cache/session-secret (gitignored).
// - Authorization: a learner WITH a password is private: their chat, quizzes and mastery need a session
//   for that learner. Learners without one (created by tools/tests via POST /api/learners) stay open
//   and are not listed in the UI.
import { createHmac, randomBytes, scrypt as scryptCb, timingSafeEqual } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { prisma, type Learner as LearnerRow } from "@sc/db";
import type { Credentials, Learner } from "@sc/schemas";
import { requireCourse } from "./course";
import { AppError, badRequest, notFound, notReady } from "./errors";
import { repoRoot } from "./storage";

/** Name of the empty notebook every new account starts with (renamed by creating named ones). */
export const FIRST_NOTEBOOK_NAME = "My first notebook";

export const SESSION_COOKIE = "omnitutor_session";
export const SESSION_TTL_S = 7 * 24 * 3600;

export const unauthorized = (message: string) => new AppError("unauthorized", message);

const toLearner = (l: LearnerRow): Learner => ({ id: l.id, name: l.name, createdAt: l.createdAt.toISOString() });

function scrypt(password: string, salt: Buffer, len: number): Promise<Buffer> {
  return new Promise((resolve, reject) => scryptCb(password.normalize("NFKC"), salt, len, (e, key) => (e ? reject(e) : resolve(key))));
}

export async function hashPassword(password: string): Promise<string> {
  const salt = randomBytes(16);
  return `scrypt$${salt.toString("base64url")}$${(await scrypt(password, salt, 32)).toString("base64url")}`;
}

export async function verifyPassword(password: string, stored: string): Promise<boolean> {
  const [alg, salt, hash] = stored.split("$");
  if (alg !== "scrypt" || !salt || !hash) return false;
  const want = Buffer.from(hash, "base64url");
  const got = await scrypt(password, Buffer.from(salt, "base64url"), want.length);
  return got.length === want.length && timingSafeEqual(got, want);
}

let cachedSecret: string | undefined;

export function sessionSecret(): string {
  if (cachedSecret) return cachedSecret;
  const fromEnv = process.env.SESSION_SECRET?.trim();
  if (fromEnv) {
    if (fromEnv.length < 32) throw new AppError("internal", "SESSION_SECRET must be at least 32 characters.");
    return (cachedSecret = fromEnv);
  }
  const file = path.join(/*turbopackIgnore: true*/ repoRoot(), "data", "cache", "session-secret");
  if (!existsSync(file)) {
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, randomBytes(32).toString("base64url"), { flag: "wx" }); // wx: never overwrite one written concurrently
  }
  return (cachedSecret = readFileSync(file, "utf8").trim());
}

const mac = (payload: string, secret: string) => createHmac("sha256", secret).update(payload).digest("base64url");

/** Pure: a session token for `learnerId`, valid until now + ttl. */
export function signSession(learnerId: string, secret: string, nowMs = Date.now(), ttlS = SESSION_TTL_S): string {
  const payload = `${learnerId}.${Math.floor(nowMs / 1000) + ttlS}`;
  return `${payload}.${mac(payload, secret)}`;
}

/** Pure: the learner id of a valid, unexpired token; null for anything else. */
export function readSession(token: string | undefined | null, secret: string, nowMs = Date.now()): string | null {
  const parts = token?.split(".");
  if (!parts || parts.length !== 3) return null;
  const [id, exp, sig] = parts as [string, string, string];
  const want = Buffer.from(mac(`${id}.${exp}`, secret));
  const got = Buffer.from(sig);
  if (got.length !== want.length || !timingSafeEqual(got, want)) return null;
  if (!/^\d+$/.test(exp) || Number(exp) * 1000 <= nowMs) return null;
  return id || null;
}

async function accountsNamed(name: string) {
  return prisma.learner.findMany({ where: { name: { equals: name.trim(), mode: "insensitive" }, passwordHash: { not: null } } });
}

export async function signup(input: Credentials): Promise<Learner> {
  if ((await accountsNamed(input.name)).length) throw badRequest(`The name "${input.name.trim()}" is taken. Sign in, or pick another name.`);
  return toLearner(await prisma.learner.create({ data: { name: input.name.trim(), passwordHash: await hashPassword(input.password) } }));
}

export async function login(input: Credentials): Promise<Learner> {
  const [account] = await accountsNamed(input.name);
  // Same work whether or not the name exists, so timing doesn't reveal account names.
  const ok = await verifyPassword(input.password, account?.passwordHash ?? "scrypt$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA");
  if (!account || !ok) throw unauthorized("Wrong name or password.");
  return toLearner(account);
}

/** The signed-in learner for a session learner id (null if signed out or the account was deleted). */
export async function sessionLearner(sessionLearnerId: string | null): Promise<Learner | null> {
  if (!sessionLearnerId) return null;
  const l = await prisma.learner.findUnique({ where: { id: sessionLearnerId } });
  return l?.passwordHash ? toLearner(l) : null;
}

/** 404 for an unknown learner; 401 when the learner is a private account and the session isn't theirs. */
export async function authorizeLearner(learnerId: string, sessionLearnerId: string | null): Promise<void> {
  const l = await prisma.learner.findUnique({ where: { id: learnerId }, select: { passwordHash: true } });
  if (!l) throw notFound(`Learner "${learnerId}" not found.`);
  if (l.passwordHash && sessionLearnerId !== learnerId) throw unauthorized("Sign in to this account to see its data.");
}

/** Quiz routes: the quiz's learner must be accessible (unknown quiz → the service's own 404). */
export async function authorizeAssessment(assessmentId: string, sessionLearnerId: string | null): Promise<void> {
  const a = await prisma.assessment.findUnique({ where: { id: assessmentId }, select: { learnerId: true } });
  if (a) await authorizeLearner(a.learnerId, sessionLearnerId);
}

/**
 * Course privacy: each account has its own course (created on first use); everything uploaded,
 * built, asked or quizzed while signed in lives there. A course is accessible exactly when its owner
 * is the session's learner (ownerless shared courses: only without a session, i.e. tools and tests).
 * Returns the course id to use for this request.
 */
export async function courseFor(sessionLearnerId: string | null, requested?: string): Promise<string> {
  if (requested) {
    const course = await prisma.course.findUnique({ where: { id: requested }, select: { ownerId: true } });
    if (!course) throw notReady(`Course "${requested}" not found.`);
    if ((course.ownerId ?? null) !== sessionLearnerId) throw unauthorized("This course belongs to another account.");
    return requested;
  }
  if (!sessionLearnerId) return requireCourse();
  return ownCourse(sessionLearnerId);
}

/**
 * The account's active notebook (an owned Course). The stored pointer is validated on every read; if
 * it is missing or stale the newest owned notebook becomes active, and an account with none gets an
 * empty first notebook named FIRST_NOTEBOOK_NAME. A per-account advisory lock makes two
 * first requests at once create one notebook, not two.
 */
export async function ownCourse(learnerId: string): Promise<string> {
  const learner = await prisma.learner.findUnique({ where: { id: learnerId }, select: { passwordHash: true, activeCourseId: true } });
  if (!learner?.passwordHash) throw unauthorized("Your session has expired. Sign in again.");
  if (learner.activeCourseId) {
    const active = await prisma.course.findFirst({ where: { id: learner.activeCourseId, ownerId: learnerId }, select: { id: true } });
    if (active) return active.id;
  }
  return prisma.$transaction(
    async (tx) => {
      await tx.$executeRaw`SELECT pg_advisory_xact_lock(hashtext(${learnerId}))`;
      const newest = await tx.course.findFirst({ where: { ownerId: learnerId }, orderBy: { createdAt: "desc" }, select: { id: true } });
      // A clean, generic first notebook: nothing is inherited from the shared evaluation course.
      const name = FIRST_NOTEBOOK_NAME;
      const id = newest?.id ?? (await tx.course.create({ data: { name, ownerId: learnerId }, select: { id: true } })).id;
      await tx.learner.update({ where: { id: learnerId }, data: { activeCourseId: id } });
      return id;
    },
    { maxWait: 10_000, timeout: 10_000 },
  );
}

/** Source routes (delete, ingest, file): the source's course must be accessible to this session (unknown source → the service's 404). */
export async function authorizeSource(sourceId: string, sessionLearnerId: string | null): Promise<void> {
  const source = await prisma.source.findUnique({ where: { id: sourceId }, select: { course: { select: { ownerId: true } } } });
  if (source && (source.course.ownerId ?? null) !== sessionLearnerId) throw unauthorized("This file belongs to another account.");
}
