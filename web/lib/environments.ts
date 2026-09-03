export const environmentIds = [
  "buyer_seller",
  "calendar",
  "negotiation",
  "word_guess",
];

/**
 * Environments whose own replay page can render one *persisted* episode, keyed
 * by the directory the server serves it from.
 *
 * The standard lane view is what every environment gets out of the box; this
 * is the opt-in seam beside it. Membership is deliberately narrow: the other
 * replay pages are Redis-stream debuggers that take a stream id rather than an
 * episode, so mounting them here would show an empty panel and assert that the
 * environment ships a viewer it does not.
 */
export const specialisedViewers: Record<string, string> = {
  negotiation: "negotiation",
};

export function specialisedViewerUrl(environmentId: string | null, episodeUid: string): string | null {
  const directory = environmentId ? specialisedViewers[environmentId] : undefined;
  if (!directory || !episodeUid) return null;
  return `/environment-replays/${directory}/?embed=1&episode=${encodeURIComponent(episodeUid)}`;
}
