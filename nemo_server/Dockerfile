FROM nanofab/nemo_splash_pad

# Install dependencies for our signal handler (POST to the daemon, load plugin_config.yaml)
RUN pip install requests pyyaml

# Copy our plugin into the container where Python can find it
COPY ./plugin /nemo/labfiles_plugin

# Patch INSTALLED_APPS directly into the splash pad settings file
RUN echo "\nINSTALLED_APPS = INSTALLED_APPS + ['labfiles_plugin']" >> /nemo/splash_pad_settings.py
