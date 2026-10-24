"use strict";
// The legacy console is a classic-script page. Expose the same strict image
// renderer used by /app so both entry points share one validation path.
import * as Images from "./app/image.js";

window.NexusImages = Images;
