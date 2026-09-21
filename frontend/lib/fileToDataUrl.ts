const MAX_IMAGE_BYTES = 4 * 1024 * 1024; // 4MB — generous for a chat attachment, small enough for free-tier request bodies

export async function fileToDataUrl(file: File): Promise<string> {
  if (!file.type.startsWith("image/")) {
    throw new Error(`${file.name} isn't an image.`);
  }
  if (file.size > MAX_IMAGE_BYTES) {
    throw new Error(`${file.name} is too large (max 4MB).`);
  }

  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(new Error(`Couldn't read ${file.name}.`));
    reader.readAsDataURL(file);
  });
}
