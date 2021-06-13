import numpy as np
import torch
from datasets import Dataset
from PIL import Image

import clip

device = "cuda" if torch.cuda.is_available() else "cpu"


class CLIP:
    def __init__(self):
        self.model, self.preprocess = clip.load("ViT-B/32", device=device, jit=False)
        if device == "cpu":
            self.model = torch.quantization.quantize_dynamic(self.model, dtype=torch.qint8)
        self.cosine_similarity = torch.nn.CosineSimilarity(dim=1, eps=1e-6)
        self.categories = ["neutral","selfie", "illustration, drawing", "toys, play, kids, children", "teddy bear, puppet", "animal, bird, mammal, insect" "fashion, clothes", "logo, commercial, ad, advertisement", "drawing, painting","anime, cartoon","comedy, fun","romance, love story","thriller, suspense, crime story","action, action movie", "horror, monster movie", "documentary", "news, journalism", "entertainment", "talk show", "porn, sex, sperm, nipples, breats, tits, boops, penis, dick, cock, clitoris, vagina, fuck, lust, horny, sexual, lick, licking",  "porn, sex, sperm, nipples", "porn, sex, sperm, penis, dick, cock", "nipples, breats, tits, boops, sexy", "penis, dick, cock", "clitoris, vagina", "sex, fuck, lust, horny, sexual, lick, licking", "porn, sex, sexy","sexy, hot","sperm, skin","lust, horny, sexual","lick, licking, body", "anime, hentai, sexy", "cartoon, sexy, sex", "hentai", "anime, sexy, breasts", "hentai"]
        self.underaged_categories = ["teenager, teen", "kid, child, teenager, teen, baby or toddler, underaged, little girl, little boy", "kid, child, little girl, little boy", "baby, toddler","adult, woman, man, grownup, grown person,full-aged of legal age","full-aged, of legal age, adult","woman, man","adult, woman, man, grownup, grown person,full-aged of legal age"]
        self.animal_categories = ["lifeless object, thing", "thing, object", "material", "furniture","wall", "house", "tree", "wood","ground","industry", "table", "bed", "tool", "dress, clothes", "door", "chair", "rock, stone", "human", "man", "woman", "man, woman", "animal","cat","dog", "cow", "pig", "goat", "sheep", "elephant", "horse", "horse, elephant, pig, dog, cat, sheep, goat, animal", "life", "wildlife"]

    def load_img(self, path):
        image = Image.open(path)
        rgbimg = Image.new("RGB", image.size)
        rgbimg.paste(image)
        return rgbimg

    def _preprocess_images(self, batch):
        similarity = []
        images = [
            self.preprocess(self.load_img(path)).unsqueeze(0).to(device)
            for path in batch["PATH"]
        ]
        texts = [clip.tokenize(text[:77]).to(device) for text in batch["TEXT"]]

        with torch.no_grad():
            image_features = self.model.encode_image(
                torch.cat([x.float() for x in images])
            )
            text_features = self.model.encode_text(torch.cat([x for x in texts]))

        for image_feat, text_feat in zip(image_features, text_features):
            # XXX Not sure about this one
            similarity.append(
                float(
                    self.cosine_similarity(
                        torch.reshape(text_feat, (1, 512)),
                        torch.reshape(image_feat, (1, 512)),
                    )
                )
            )

        batch["similarity"] = similarity
        batch["image_features"] = image_features.detach().cpu().numpy()
        return batch

    def preprocess_images(self, df):
        im_dataset = Dataset.from_pandas(df)
        im_dataset = im_dataset.map(self._preprocess_images, batched=True, batch_size=8)
        return im_dataset["image_features"], im_dataset["similarity"]

    def filter(self, img_embeddings, classes):
        ret = []
        text = self.model.encode_text(clip.tokenize(classes).to(device))

        with torch.no_grad():
            for emb in img_embeddings:
                logits_per_image, _ = self.model(
                    torch.as_tensor(emb).to(device), text.float()
                )
                probs = logits_per_image.softmax(dim=-1).cpu().numpy()
                ret.append(np.argmax(probs))
        return ret
