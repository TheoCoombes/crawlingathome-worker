#!/bin/bash

git clone "https://github.com/TheoCoombes/crawlingathome" crawlingathome_client
pip3 install -r crawlingathome_client/requirements.txt --no-cache-dir
pip3 install -r requirements.txt --no-cache-dir
pip3 install git+https://github.com/Wikidepia/CLIP --no-cache-dir
