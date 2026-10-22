#include <errno.h>
#include <string.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <netdb.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <netinet/in.h>

int extract_message(char **buf, char **msg)
{
    char    *newbuf;
    int    i;

    *msg = 0;
    if (*buf == 0)
        return (0);
    i = 0;
    while ((*buf)[i])
    {
        if ((*buf)[i] == '\n')
        {
            newbuf = calloc(1, sizeof(*newbuf) * (strlen(*buf + i + 1) + 1));
            if (newbuf == 0)
                return (-1);
            strcpy(newbuf, *buf + i + 1);
            *msg = *buf;
            (*msg)[i + 1] = 0;
            *buf = newbuf;
            return (1);
        }
        i++;
    }
    return (0);
}

char *str_join(char *buf, char *add)
{
    char    *newbuf;
    int        len;

    if (buf == 0)
        len = 0;
    else
        len = strlen(buf);
    newbuf = malloc(sizeof(*newbuf) * (len + strlen(add) + 1));
    if (newbuf == 0)
        return (0);
    newbuf[0] = 0;
    if (buf != 0)
        strcat(newbuf, buf);
    free(buf);
    strcat(newbuf, add);
    return (newbuf);
}

int     next_id = 0;
int     max_fd = 0;
int     sockfd = 0;
int     client_id[65536];
char    *client_buf[65536];
fd_set  all_set, read_set;
char    recv_buf[1001];
char    msg_buf[64];

void err(void)
{
    write(2, "Fatal error\n", 12);
    exit(1);
}

void broadcast(int from, char *str)
{
    for (int fd = 0; fd <= max_fd; fd++)
    {
        if (fd != from && fd != sockfd && FD_ISSET(fd, &all_set))
            send(fd, str, strlen(str), MSG_NOSIGNAL);
    }
}

int main(int ac, char **av) {
    int connfd, len, n, ret;
    struct sockaddr_in servaddr, cli;
    char *line, *out;

    if (ac != 2) {
        write(2, "Wrong number of arguments\n", 26);
        exit(1);
    }

    sockfd = socket(AF_INET, SOCK_STREAM, 0);
    if (sockfd == -1)
        err();
    bzero(&servaddr, sizeof(servaddr));

    servaddr.sin_family = AF_INET;
    servaddr.sin_addr.s_addr = htonl(2130706433);
    servaddr.sin_port = htons(atoi(av[1]));

    if ((bind(sockfd, (const struct sockaddr *)&servaddr, sizeof(servaddr))) != 0)
        err();
    if (listen(sockfd, 10) != 0)
        err();

    FD_ZERO(&all_set);
    FD_SET(sockfd, &all_set);
    max_fd = sockfd;

    while (1) {
        read_set = all_set;
        if (select(max_fd + 1, &read_set, NULL, NULL, NULL) < 0)
            err();

        for (int fd = 0; fd <= max_fd; fd++) {
            if (!FD_ISSET(fd, &read_set))
                continue;

            if (fd == sockfd) {
                len = sizeof(cli);
                connfd = accept(sockfd, (struct sockaddr *)&cli, (socklen_t *)&len);
                if (connfd < 0)
                    continue;
                if (connfd > max_fd)
                    max_fd = connfd;
                client_id[connfd] = next_id++;
                client_buf[connfd] = NULL;
                FD_SET(connfd, &all_set);
                sprintf(msg_buf, "server: client %d just arrived\n", client_id[connfd]);
                broadcast(connfd, msg_buf);
                continue;
            }

            n = recv(fd, recv_buf, 1000, 0);
            if (n <= 0) {
                sprintf(msg_buf, "server: client %d just left\n", client_id[fd]);
                broadcast(fd, msg_buf);
                free(client_buf[fd]);
                client_buf[fd] = NULL;
                FD_CLR(fd, &all_set);
                close(fd);
                continue;
            }
            recv_buf[n] = '\0';

            client_buf[fd] = str_join(client_buf[fd], recv_buf);
            if (!client_buf[fd])
                err();
            out = NULL;
            while ((ret = extract_message(&client_buf[fd], &line)) != 0) {
                if (ret < 0)
                    err();
                sprintf(msg_buf, "client %d: ", client_id[fd]);
                out = str_join(out, msg_buf);
                if (!out)
                    err();
                out = str_join(out, line);
                if (!out)
                    err();
                free(line);
            }
            if (out) {
                broadcast(fd, out);
                free(out);
            }
        }
    }
    return (0);
}
