#!/home/nrnelson/tumblr_dl/venv/bin/python3

import pytumblr
import os
import re
import argparse
import time
import requests
import yaml
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

def download_media(blog_name, output_dir, config_path, start_post=None, max_posts=None, debug=False):
    """
    Downloads all images and videos from a Tumblr blog using pytumblr and OAuth credentials.
    OAuth credentials are read from a YAML config file, but the blog name, output directory,
    and config file path can be overridden via command-line arguments.

    This version attempts to handle all post types and adds file type counting.

    Args:
        blog_name (str): The name of the Tumblr blog (e.g., "example").
        output_dir (str): The directory to save the downloaded media.
        config_path (str): The path to the YAML config file containing OAuth keys.
        start_post (int, optional): The post ID to start downloading from.
                                    If None, starts from the beginning. Defaults to None.
        max_posts (int, optional): The maximum number of posts to download.
                                   If None, downloads all posts. Defaults to None.
        debug (bool, optional): Enable debug logging.  Defaults to False.
    """

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Read OAuth credentials from YAML config file
    try:
        with open(os.path.expanduser(config_path), "r") as f:
            config = yaml.safe_load(f)
            consumer_key = config["consumer_key"]
            consumer_secret = config["consumer_secret"]
            oauth_token = config["oauth_token"]
            oauth_secret = config["oauth_token_secret"]
    except FileNotFoundError:
        print(f"Error: {config_path} file not found.  Make sure it exists and has the correct format.")
        return
    except KeyError as e:
        print(f"Error: Missing key in {config_path} file: {e}.  Make sure it includes consumer_key, consumer_secret, oauth_token, and oauth_token_secret.")
        return
    except Exception as e:
        print(f"Error reading {config_path}: {e}")
        return

    client = pytumblr.TumblrRestClient(
        consumer_key,
        consumer_secret,
        oauth_token,
        oauth_secret,
    )

    offset = 0
    downloaded_count = 0
    post_count = 0

    # Initialize file type counts
    file_type_counts = {"image": 0, "video": 0, "audio": 0, "other": 0, "existing": 0}
    file_type_downloaded = {"image": 0, "video": 0, "audio": 0, "other": 0}


    print(f"Starting download from {blog_name} to {output_dir}")

    if start_post:
        offset = start_post

    while True:
        try:
            posts = client.posts(blog_name, offset=offset, limit=20)
            if not posts['posts']:
                print("No more posts found.")
                break

            for post in posts['posts']:
                post_count += 1
                print(f"Processing post {post_count} (ID: {post['id']})...")
                if debug:
                    print(f"Post type: {post['type']}")

                #Process different post types.
                if post['type'] == 'photo':
                    if 'photos' in post:
                        for photo in post['photos']:
                            download_url = photo['original_size']['url']
                            if debug:
                                print(f"Photo URL: {download_url}")
                            downloaded_status = download_file(download_url, output_dir, blog_name, debug)
                            if downloaded_status == 1:
                                file_type_downloaded["image"] += 1 #Count downloads.
                            elif downloaded_status ==0:
                                file_type_counts["existing"]+=1
                            file_type_counts["image"] += 1 #Count all file types found
                    elif 'photo_url' in post:
                        download_url = post['photo_url']
                        if debug:
                            print(f"Photo URL: {download_url}")
                        downloaded_status = download_file(download_url, output_dir, blog_name, debug)
                        if downloaded_status == 1:
                            file_type_downloaded["image"] += 1 #Count downloads.
                        elif downloaded_status ==0:
                            file_type_counts["existing"]+=1
                        file_type_counts["image"] += 1 #Count all file types found
                    else:
                        print("No photo URL found in photo post.")
                elif post['type'] == 'video':
                    if 'video_url' in post:
                        download_url = post['video_url']
                        if debug:
                            print(f"Video URL: {download_url}")
                        downloaded_status = download_file(download_url, output_dir, blog_name, debug)
                        if downloaded_status == 1:
                            file_type_downloaded["video"] += 1 #Count downloads.
                        elif downloaded_status ==0:
                            file_type_counts["existing"]+=1
                        file_type_counts["video"] += 1 #Count all file types found

                    elif 'player' in post:
                        player_html = post['player'][0]['embed_code']
                        soup = BeautifulSoup(player_html, 'html.parser')
                        iframe = soup.find('iframe')
                        if iframe and iframe.has_attr('src'):
                            video_url = iframe['src']
                            if debug:
                                print(f"iFrame Video URL: {video_url}")
                            downloaded_status = download_file(video_url, output_dir, blog_name, debug)
                            if downloaded_status == 1:
                                file_type_downloaded["video"] += 1 #Count downloads.
                            elif downloaded_status ==0:
                                file_type_counts["existing"]+=1
                            file_type_counts["video"] += 1 #Count all file types found

                    else:
                        print("No video URL found in video post.")
                elif post['type'] == 'audio':
                    if 'audio_url' in post:
                        download_url = post['audio_url']
                        if debug:
                            print(f"Audio URL: {download_url}")
                        downloaded_status = download_file(download_url, output_dir, blog_name, debug)
                        if downloaded_status == 1:
                            file_type_downloaded["audio"] += 1 #Count downloads.
                        elif downloaded_status ==0:
                            file_type_counts["existing"]+=1
                        file_type_counts["audio"] += 1 #Count all file types found

                elif post['type'] == 'text': #If Text, attempt to extract images
                    body = post.get('body', '')
                    soup = BeautifulSoup(body, 'html.parser')
                    images = soup.find_all('img')
                    for img in images:
                        src = img.get('src')
                        if src:
                            if debug:
                                print(f"Found image with src: {src}")
                            downloaded_status = download_file(src, output_dir, blog_name, debug)
                            if downloaded_status == 1:
                                file_type_downloaded["image"] += 1 #Count downloads.
                            elif downloaded_status ==0:
                                file_type_counts["existing"]+=1
                            file_type_counts["image"] += 1 #Count all file types found
                        else:
                            if debug:
                                print("Image tag has no src attribute.")
                else:
                    print(f"Unhandled post type: {post['type']}")

                if max_posts and post_count >= max_posts:
                    print(f"Reached maximum posts ({max_posts}). Stopping.")
                    break

            offset += 20
            time.sleep(1)

            if max_posts and post_count >= max_posts:
                break

        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            break

    print(f"Downloaded {downloaded_count} files from {blog_name}.")
    print(f"Processed {post_count} posts.")

    #Print overall Counts:
    print("\n---File Type Summary---")
    for file_type, count in file_type_counts.items():
        print(f"{file_type.capitalize()}: {count} (Downloaded: {file_type_downloaded.get(file_type, 0)}, Existing: {count-file_type_downloaded.get(file_type, 0)})") #Added print statement


def download_file(url, output_dir, blog_name, debug=False):
    """Downloads a single file from the given URL."""
    if debug:
        print("Entering Download File...")
    try:
        filename = os.path.join(output_dir, sanitize_filename(os.path.basename(url)))
        if os.path.exists(filename):
            if debug:
                print(f"Skipping: {filename} (already exists)")
            return 0 #File Exists

        print(f"Downloading: {url} to {filename}")
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                   'Referer': f'https://{blog_name}.tumblr.com/'}
        response = requests.get(url, stream=True, headers=headers)
        response.raise_for_status()

        with open(filename, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return 1 #Successful Download

    except requests.exceptions.RequestException as e:
        print(f"Download failed for {url}: {e}")
        return -1 #Failed

    except Exception as e:
        print(f"An unexpected error occurred during download of {url}: {e}")
        return -1 #Failed


def sanitize_filename(filename):
    """Sanitizes a filename to remove invalid characters."""
    name, ext = os.path.splitext(filename)
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    name = name[:190]
    return name + ext


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download media from a Tumblr blog.")
    parser.add_argument("blog_name", nargs='?', default=None, help="The name of the Tumblr blog (e.g., 'example')")
    parser.add_argument("output_dir", nargs='?', default=None, help="The directory to save the downloaded media")
    parser.add_argument("--config", default="~/.tumblr", help="Path to the YAML config file (default: ~/.tumblr)")
    parser.add_argument("--start_post", type=int, help="The post ID to start downloading from (optional)", default=None)
    parser.add_argument("--max_posts", type=int, help="The maximum number of posts to download (optional)", default=None)
    parser.add_argument("--debug", action="store_true", help="Enable debug logging (optional)")

    args = parser.parse_args()

    # Check for positional arguments. These need to be specified or the rest can't be found.
    if args.blog_name is None or args.output_dir is None:
        parser.error("blog_name and output_dir are required arguments")

    download_media(args.blog_name, args.output_dir, args.config, args.start_post, args.max_posts, args.debug)

