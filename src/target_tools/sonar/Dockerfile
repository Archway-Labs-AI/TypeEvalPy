FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /build
COPY harness/pom.xml ./pom.xml
RUN mvn -B -q dependency:go-offline || true
COPY harness/src ./src
RUN mvn -B -q -DskipTests package

FROM eclipse-temurin:17-jre-noble
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /build/target/sonar-typeeval-runner.jar /app/sonar-typeeval-runner.jar
COPY src /tmp/src
CMD ["bash"]
