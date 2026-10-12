"use client";

import { ImageReference } from "../lib/api";

export function MediaPreview({ value, environmentId, episodeUid, caption }: {
  value: unknown; environmentId: string; episodeUid?: string; caption?: string;
}) {
  const images: ImageReference[] = [];
  function visit(node: unknown) {
    if (Array.isArray(node)) node.forEach(visit);
    else if (node && typeof node === "object") {
      const image = node as ImageReference;
      if (image.type === "image" && /^[0-9a-f]{64}$/.test(image.sha256) && image.asset === `asset://${image.sha256}`) images.push(image);
      else Object.values(node).forEach(visit);
    }
  }
  visit(value);
  if (!images.length) return null;
  const base = episodeUid ? `/api/episodes/${encodeURIComponent(episodeUid)}` : `/api/environments/${encodeURIComponent(environmentId)}`;
  return <figure className="media-preview"><figcaption>{caption ?? "Recorded image input"}</figcaption>
    {images.map((image, index) => <img key={`${image.sha256}-${index}`} style={{ maxWidth: "100%", maxHeight: 360, objectFit: "contain" }}
      src={`${base}/assets/${image.sha256}`} alt="Recorded image input" />)}
  </figure>;
}
