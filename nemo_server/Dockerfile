FROM nanofab/nemo_splash_pad

# Install requests so our signal handler can POST to the daemon
RUN pip install requests

# Copy our plugin into the container where Python can find it
COPY ./plugin /nemo/labfiles_plugin

# Patch INSTALLED_APPS directly into the splash pad settings file
RUN echo "\nINSTALLED_APPS = INSTALLED_APPS + ['labfiles_plugin']" >> /nemo/splash_pad_settings.py
