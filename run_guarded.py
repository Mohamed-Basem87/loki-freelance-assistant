from app.notification_guard.integration import install


# Install the notification guard before the normal application starts.
#
# install() imports app.job_processor and replaces only its in-memory
# notification references. No existing application source file is edited.

install()


from app.bot import main


if __name__ == "__main__":
    main()
