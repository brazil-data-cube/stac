#!/usr/bin/env bash
#
# This file is part of Brazil Data Cube STAC.
# Copyright (C) 2019 INPE.
#
# Brazil Data Cube STAC is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
#

##### DEPLOY
echo
echo "BUILD STARTED"
echo
echo "NEW TAG - BDC-STAC:"
read BDC_STAC_TAG
IMAGE_BDC_STAC="registry.dpi.inpe.br/brazildatacube/bdc-stac/0.9.0"
IMAGE_BDC_STAC_FULL="${IMAGE_BDC_STAC}:${BDC_STAC_TAG}"
docker build -t ${IMAGE_BDC_STAC_FULL} . --no-cache
docker push ${IMAGE_BDC_STAC_FULL}
