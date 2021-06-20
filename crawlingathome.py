import gc
import os
import pickle
import shutil
import time
from copy import copy
from io import BytesIO
from urllib.parse import urljoin

import ujson
from many_requests import ManyRequests
from PIL import Image, ImageFile, UnidentifiedImageError

ImageFile.LOAD_TRUNCATED_IMAGES = True  # https://stackoverflow.com/a/47958486


def chunk_using_generators(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def remove_bad_chars(text):
    return "".join(c for c in text if c.isprintable())


def parse_wat(content, start, line_count):
    import ftfy
    import pycld2 as cld2

    blocklist = open("blocklist-domain.txt").read().splitlines()
    valid_data = []
    content.seek(start)
    for _ in range(line_count):
        line = content.readline()
        if "IMG@" not in line:
            continue
        line_str = line.strip()
        data = ujson.loads(line_str)
        linklist = data["Envelope"]["Payload-Metadata"]["HTTP-Response-Metadata"][
            "HTML-Metadata"
        ]["Links"]
        base_url = os.path.dirname(
            data["Envelope"]["WARC-Header-Metadata"]["WARC-Target-URI"]
        )  # get base url
        license = "?"
        for e in linklist:
            if "url" in e and "creativecommons.org/licenses/" in e["url"]:
                license = e["url"]
            if "alt" not in e:
                continue
            url = e["url"]
            alt_text = ftfy.fix_text(e["alt"].replace("\n", " ")).strip()
            if any(
                x in url for x in [".svg", ".gif", "data:image", "javascript:"]
            ) or any(bl in url for bl in blocklist):
                continue
            try:
                _, _, details = cld2.detect(alt_text)
            except Exception as e:
                alt_text = remove_bad_chars(alt_text)
                _, _, details = cld2.detect(alt_text)

            if details[0][1] == "en":
                if not url.startswith("http"):
                    url = urljoin(base_url, url)
                valid_data.append((url, alt_text, license))
    return [
        t for t in {tuple(i) for i in valid_data}
    ]  # Remove duplicate tuple from list


def process_img_content(response, alt_text, license, sample_id):
    img_output_folder = "save/images/"

    try:
        if len(response.content) < 5000:
            return
        img_data = BytesIO(response.content)
        with Image.open(img_data) as im:
            width, height = im.size
            im_format = im.format
            out_fname = f"{img_output_folder}{str(sample_id)}.{im_format.lower()}"
            if im_format not in ["JPEG", "JPG", "PNG"]:
                return
            if im.mode != "RGB":
                im = im.convert("RGB")
            im.save(out_fname)
    except (KeyError, UnidentifiedImageError):
        return

    return [str(sample_id), out_fname, response.url, alt_text, width, height, license]


async def dl_wat(valid_data, first_sample_id):
    import pandas as pd
    import tractor

    # Download every image available
    processed_samples = []
    responses = ManyRequests(n_workers=5, n_connections=5)(
        method="GET", url=[data[0] for data in valid_data]
    )

    for i, (resp, data) in enumerate(zip(responses, valid_data)):
        processed_samples.append(process_img_content(resp, data[1], data[2], i))
    return pd.DataFrame(
        processed_samples,
        columns=["SAMPLE_ID", "PATH", "URL", "TEXT", "HEIGHT", "WIDTH", "LICENSE"],
    )


def df_clipfilter(df):
    sim_threshold = 0.3
    underaged_text = ["teen", "kid", "child", "baby"]
    import clip_filter

    clip = clip_filter.CLIP()
    img_embedding, similarities = clip.preprocess_images(df)
    tmp_embed = copy(img_embedding)
    for i, img_embed in enumerate(tmp_embed):
        if similarities[i] < sim_threshold:
            df.drop(i, inplace=True)
            img_embedding.remove(img_embed)
            continue

        # get most similar categories
        nsfw_prob = clip.prob(img_embed, clip.categories)
        df.at[i, "NSFW"] = "UNSURE"
        df.at[i, "similarity"] = similarities[i]
        if nsfw_prob[0] < 19 and nsfw_prob[1] < 19:
            df.at[i, "NSFW"] = "UNLIKELY"
            continue
        elif nsfw_prob[0] >= 19 and nsfw_prob[1] >= 19:
            df.at[i, "NSFW"] = "NSFW"

        underage_prob = clip.prob(img_embed, clip.underaged_categories)
        if (
            underage_prob[0] < 4
            or underage_prob[1] < 4
            or any(x in df.at[i, "TEXT"] for x in underaged_text)
        ):
            df.drop(i, inplace=True)
            img_embedding.remove(img_embed)
            continue

        animal_prob = clip.prob(img_embed, clip.animal_categories)
        if animal_prob[0] > 20:
            df.drop(i, inplace=True)
            img_embedding.remove(img_embed)

    df.reset_index(drop=True, inplace=True)
    return df, img_embedding


def df_tfrecords(df, output_fname):
    import tensorflow as tf
    from tfr_image.utils import bytes_feature, int64_feature

    def image_to_tfexample(sample_id, image_data, image_format, height, width, caption):
        return tf.train.Example(
            features=tf.train.Features(
                feature={
                    "sampleID": bytes_feature(sample_id),
                    "image": bytes_feature(image_data),
                    "format": bytes_feature(image_format),
                    "label": bytes_feature(caption),
                    "height": int64_feature(height),
                    "width": int64_feature(width),
                }
            )
        )

    with tf.io.TFRecordWriter(output_fname) as tfrecord_writer:
        for i in range(len(df)):
            df_image = df.iloc[i]
            image_fname = df_image["PATH"]
            file_type = image_fname.split(".")[-1]
            with tf.io.gfile.GFile(image_fname, "rb") as f:
                image_data = f.read()
            example = image_to_tfexample(
                str(df_image["SAMPLE_ID"]).encode("utf_8"),
                image_data,
                file_type.encode("utf_8"),
                df_image["HEIGHT"],
                df_image["WIDTH"],
                df_image["TEXT"].encode("utf_8"),
            )
            tfrecord_writer.write(example.SerializeToString())


def upload_gdrive(output_filename):
    import requests

    client_id = (
        "648172777761-onv1nc5f93nhlhf63flsq6onrmjphpfo.apps.googleusercontent.com"
    )
    client_secret = "HZ4Zw-_jVJ-3mwicz1NM5W5x"
    refresh_token = "1//04N2Kysz1LObLCgYIARAAGAQSNwF-L9IrntHNWi2_nEVu2QX5fmlW0Ea0qA-ToBJLSdatDATYxiKcNFI8eZQ_fYN53gjF7b8MGmA"

    def refresh_gdrive_token():
        params = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }

        authorization_url = "https://www.googleapis.com/oauth2/v4/token"

        r = requests.post(authorization_url, data=params)

        if r.ok:
            return r.json()["access_token"]
        else:
            return None

    access_t = refresh_gdrive_token()
    headers = {"Authorization": "Bearer " + access_t}
    para = {
        "name": output_filename.split("/")[-1],
        "parents": ["1CIgcIR7nX2xNBPB577jwEqbbwxAJR_nt"],
    }

    files = {
        "data": ("metadata", ujson.dumps(para), "application/json; charset=UTF-8"),
        "file": ("application/zip", open(output_filename, "rb")),
    }
    requests.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
        headers=headers,
        files=files,
    )


class FileData:
    def __init__(self, filename):
        self._filename = filename
        self._line_to_position = [0]
        self._length = 0

        with open(self._filename, "r") as f:
            while f.readline():
                self._line_to_position.append(f.tell())
                self._length += 1

    def __getitem__(self, line):
        return self._line_to_position[line]

    def __len__(self):
        return self._length


if __name__ == "__main__":
    import crawlingathome_client as cah

    YOUR_NICKNAME_FOR_THE_LEADERBOARD = "Wikipeadi"
    CRAWLINGATHOME_SERVER_URL = "http://178.63.68.247:8181/"

    client = cah.init(
        url=CRAWLINGATHOME_SERVER_URL, nickname=YOUR_NICKNAME_FOR_THE_LEADERBOARD
    )
    output_folder = "./save/"
    csv_output_folder = output_folder
    img_output_folder = output_folder + "images/"

    while client.jobCount() > 0:
        start = time.time()
        if os.path.exists(output_folder):
            shutil.rmtree(output_folder)
        if os.path.exists(".tmp"):
            shutil.rmtree(".tmp")

        os.mkdir(output_folder)
        os.mkdir(img_output_folder)
        os.mkdir(".tmp")

        client.newJob()
        client.downloadShard()
        first_sample_id = int(client.start_id)
        last_sample_id = int(client.end_id)
        shard_of_chunk = client.shard_piece

        out_fname = f"FIRST_SAMPLE_ID_IN_SHARD_{str(first_sample_id)}_LAST_SAMPLE_ID_IN_SHARD_{str(last_sample_id)}_{shard_of_chunk}"
        print(
            f"[crawling@home] shard identification {out_fname}"
        )  # in case test fails, we need to remove bad data
        client.log("Processing shard")

        fd = FileData("shard.wat")

        if shard_of_chunk == 0:
            start_index = fd[0]
        if shard_of_chunk == 1:
            start_index = fd[int(len(fd) * 0.5)]

        lines = int(len(fd) * 0.5)

        with open("shard.wat", "r") as infile:
            parsed_data = parse_wat(infile, start_index, lines)

        client.log("Downloading images")
        dlparse_df = dl_wat(parsed_data, first_sample_id)
        dlparse_df.to_csv(output_folder + out_fname + ".csv", index=False, sep="|")

        client.log("Dropping NSFW keywords")
        filtered_df, img_embeddings = df_clipfilter(dlparse_df)
        filtered_df.to_csv(output_folder + out_fname + ".csv", index=False, sep="|")
        img_embeds_sampleid = {}
        for i, img_embed_it in enumerate(img_embeddings):
            dfid_index = filtered_df.at[i, "SAMPLE_ID"]
            img_embeds_sampleid[str(dfid_index)] = img_embed_it
        with open(f"{output_folder}image_embedding_dict-{out_fname}.pkl", "wb") as f:
            pickle.dump(img_embeds_sampleid, f)

        client.log("Saving TFRs")
        print(f"[crawling@home] downloaded images: {len(dlparse_df)}")
        print(f"[crawling@home] filtered pairs: {len(filtered_df)}")
        df_tfrecords(
            filtered_df,
            f"{output_folder}crawling_at_home_{out_fname}__00000-of-00001.tfrecord",
        )

        client._markjobasdone(len(filtered_df))
        print(f"[crawling@home] jobs completed in {round(time.time() - start)} seconds")
