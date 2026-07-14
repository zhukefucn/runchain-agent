const cancellers = new Set<() => Promise<void> | void>();

export function registerStreamCanceller(cancel: () => Promise<void> | void) {
  cancellers.add(cancel);
  return () => cancellers.delete(cancel);
}

export async function abortAllStreams() {
  await Promise.allSettled([...cancellers].map((cancel) => cancel()));
}
