"use client";

import { ImageReference } from "../lib/api";

export function MediaPreview({ value, environmentId }: { value: unknown; environmentId: string }) {
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
  return <figure><figcaption>Researcher inspection · agent visibility follows the recorded seat observation</figcaption>
    {images.map((image, index) => <img key={`${image.sha256}-${index}`} style={{ maxWidth: 320, maxHeight: 240, objectFit: "contain" }}
      src={`/api/environments/${encodeURIComponent(environmentId)}/assets/${image.sha256}`} alt="Recorded image input" />)}
  </figure>;
}
